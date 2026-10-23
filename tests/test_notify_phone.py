import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPT = pathlib.Path(__file__).parents[1] / 'scripts/notify-phone.py'
spec = importlib.util.spec_from_file_location('notify_phone', SCRIPT)
phone = importlib.util.module_from_spec(spec)
spec.loader.exec_module(phone)


class PhoneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = pathlib.Path(self.temp.name)
        self.config = self.home / 'notify.env'
        self.config.write_text('NTFY_TOPIC=test-topic\nIDLE_NOTIFY_SECONDS=0\nBACKGROUND_WAIT_LIMIT_SECONDS=0\n')
        self.log = self.home / 'requests.jsonl'
        binary = self.home / 'bin'
        binary.mkdir()
        curl = binary / 'curl'
        curl.write_text('#!' + sys.executable + '\nimport json,os,sys\n'
                        'with open(os.environ["CURL_LOG"],"a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\n'
                        'sys.exit(int(os.environ.get("CURL_EXIT","0")))\n')
        curl.chmod(0o700)
        self.env = patch.dict(os.environ, {'HOME': str(self.home), 'CODEX_HOME': str(self.home / '.codex'),
            'CODEX_NOTIFY_CONFIG': str(self.config), 'CODEX_NOTIFY_STATE_DIR': str(self.home / 'state'),
            'PATH': str(binary) + os.pathsep + os.environ['PATH'], 'CURL_LOG': str(self.log)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.transcript = self.home / 'rollout.jsonl'
        self.transcript.write_text(json.dumps({'type': 'session_meta', 'payload': {
            'source': 'cli', 'cwd': str(self.home / 'original-project')}}) + '\n')

    def event(self, kind, **extra):
        return dict(session_id='test-session', cwd=str(self.home / 'changed-project'),
                    transcript_path=str(self.transcript), hook_event_name=kind, **extra)

    def read_state(self, session='test-session'):
        with phone.locked(phone.state_directory(session)) as state:
            return dict(state)

    def prompt(self, text='Please continue', **extra):
        phone.handle(self.event('UserPromptSubmit', prompt=text, **extra), phone.load_config())

    def stop(self):
        with patch.object(phone, 'launch_worker'):
            phone.handle(self.event('Stop', last_assistant_message='PRIVATE MODEL TEXT'), phone.load_config())
        return self.read_state().get('pending', {}).get('token')

    def deliver(self, token):
        if token:
            phone.worker('test-session', token, phone.load_config())

    def requests(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_stop_delivers_once_per_human_prompt_and_preserves_origin(self):
        self.prompt()
        self.deliver(self.stop())
        self.deliver(self.stop())
        self.assertEqual(len(self.requests()), 1)
        payload = json.loads(self.requests()[0][-2])
        self.assertEqual(payload['title'], 'Codex · original-project')
        self.assertEqual(payload['message'], phone.WAITING)
        self.assertNotIn('PRIVATE MODEL TEXT', self.log.read_text())
        self.prompt('Next task')
        self.deliver(self.stop())
        self.assertEqual(len(self.requests()), 2)

    def test_cancels_for_prompt_new_stop_interrupt_session_end_and_tool_activity(self):
        for kind in ['UserPromptSubmit', 'Interrupt', 'SessionEnd', 'PreToolUse', 'Stop']:
            with self.subTest(kind=kind):
                self.prompt()
                old = self.stop()
                if kind == 'Stop':
                    self.stop()
                else:
                    phone.handle(self.event(kind, prompt='new prompt'), phone.load_config())
                self.deliver(old)
        self.assertEqual(self.requests(), [])

    def test_synthetic_messages_do_not_reset_deduplication(self):
        self.prompt()
        self.deliver(self.stop())
        for text in ['Work Worker inbox pointer (external data): {}', '<task-notification>done',
                     'Work Worker automatic startup initialization.', 'Work Worker startup signal (external service data).',
                     'Message Type: MESSAGE hi']:
            self.prompt(text)
            self.deliver(self.stop())
        self.assertEqual(len(self.requests()), 1)

    def test_service_only_session_does_not_notify(self):
        self.prompt('Work Worker automatic startup initialization.')
        self.assertIsNone(self.stop())

    def test_transcript_activity_cancels_even_without_hook(self):
        self.prompt()
        token = self.stop()
        with self.transcript.open('a') as stream:
            stream.write(json.dumps({'type': 'response_item', 'payload': {
                'type': 'message', 'role': 'assistant', 'content': 'private'}}) + '\n')
        self.deliver(token)
        self.assertEqual(self.requests(), [])

    def test_shell_task_limit_completion_old_tasks_and_listener(self):
        self.prompt()
        phone.handle(self.event('PreToolUse', tool_name='Bash', tool_use_id='job',
                               tool_input={'command': 'sleep 20'}), phone.load_config())
        self.deliver(self.stop())
        self.assertIn('background task is still running', self.log.read_text())
        self.prompt()
        phone.handle(self.event('PreToolUse', tool_name='Bash', tool_use_id='job',
                               tool_input={'command': 'sleep 20'}), phone.load_config())
        phone.handle(self.event('PostToolUse', tool_use_id='job'), phone.load_config())
        self.deliver(self.stop())
        self.assertEqual(json.loads(self.requests()[-1][-2])['message'], phone.WAITING)
        self.prompt()
        phone.handle(self.event('PreToolUse', tool_name='Bash', tool_use_id='listener',
                               tool_input={'command': 'bash devops/listen-events.sh'}), phone.load_config())
        self.deliver(self.stop())
        self.assertEqual(json.loads(self.requests()[-1][-2])['message'], phone.WAITING)

    def test_missing_config_unknown_events_exec_and_subagents_stay_quiet(self):
        self.config.unlink()
        result = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(self.event('Stop')),
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, '')
        for source in ['exec', {'subagent': 'worker'}]:
            self.transcript.write_text(json.dumps({'type': 'session_meta', 'payload': {'source': source}}))
            self.prompt()
            self.assertIsNone(self.stop())
        self.assertEqual(self.requests(), [])

    def test_delivery_config_is_literal_and_reuses_claude_fallback(self):
        os.environ.pop('CODEX_NOTIFY_CONFIG')
        fallback = self.home / '.claude/notify.env'
        fallback.parent.mkdir()
        fallback.write_text('export NTFY_TOPIC="shared-topic" # comment\nNTFY_SERVER=https://example.test\n')
        self.assertEqual(phone.load_config()['NTFY_TOPIC'], 'shared-topic')
        own = self.home / '.codex/notify.env'
        own.parent.mkdir()
        own.write_text("NTFY_TOPIC='$(touch never-execute)'\n")
        self.assertEqual(phone.load_config()['NTFY_TOPIC'], '$(touch never-execute)')
        self.assertFalse((self.home / 'never-execute').exists())

    def test_both_channels_and_ntfy_failure_does_not_skip_telegram(self):
        os.environ['CURL_EXIT'] = '22'
        config = {'NTFY_TOPIC': 't', 'NTFY_SERVER': 'https://ntfy.example.test',
                  'TELEGRAM_BOT_TOKEN': '123:fake', 'TELEGRAM_CHAT_ID': '42'}
        phone.send(config, 'Codex · проект', phone.PERMISSION)
        requests = self.requests()
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0][-1], 'https://ntfy.example.test')
        self.assertIn('chat_id=42', requests[1])
        self.assertEqual(requests[1][-1], 'https://api.telegram.org/bot123:fake/sendMessage')
        self.assertIn('--max-time', requests[0])

    def test_question_and_permission_hooks_are_advisory_and_detached(self):
        for kind, tool, body in [('PreToolUse', 'request_user_input', phone.QUESTION),
                                 ('PreToolUse', 'mcp__x__request_user_input_async', phone.QUESTION)]:
            with patch.object(phone, 'project_name', return_value='project'), patch.object(phone.subprocess, 'Popen') as spawn:
                phone.handle(self.event(kind, tool_name=tool, tool_input={'command': 'PRIVATE'}), phone.load_config())
                args = spawn.call_args[0][0]
                self.assertEqual(args[-1], body)
                self.assertNotIn('PRIVATE', ' '.join(args))
                self.assertTrue(spawn.call_args[1]['start_new_session'])

    def permission(self, command='PRIVATE COMMAND', turn='turn-1'):
        return self.event('PermissionRequest', turn_id=turn, tool_name='Bash',
                          tool_input={'command': command})

    def input_worker(self, call):
        phone.input_worker(*call.args, phone.load_config())

    def test_permission_waits_and_resolved_auto_approval_never_notifies(self):
        with patch.object(phone, 'launch_input_worker') as launch:
            phone.handle(self.permission(), phone.load_config())
        self.assertEqual(self.requests(), [])
        phone.handle(self.event('PostToolUse', tool_name='Bash', tool_use_id='call-1',
                               tool_input={'command': 'PRIVATE COMMAND'}), phone.load_config())
        self.input_worker(launch.call_args)
        self.assertEqual(self.requests(), [])

    def test_automatic_reviewer_does_not_launch_any_permission_notification(self):
        with self.transcript.open('a') as stream:
            stream.write(json.dumps({'type': 'turn_context', 'payload': {
                'approval_policy': 'on-request', 'approvals_reviewer': 'auto_review'}}) + '\n')
        with patch.object(phone, 'launch_input_worker') as launch, patch.object(phone.subprocess, 'Popen') as spawn:
            for number in range(20):
                phone.handle(self.permission(command='command-' + str(number)), phone.load_config())
        launch.assert_not_called()
        spawn.assert_not_called()
        self.assertEqual(self.read_state().get('input_pending', {}), {})

    def test_latest_approval_context_controls_routing_and_worker_rechecks_it(self):
        with patch.object(phone, 'launch_input_worker') as launch:
            phone.handle(self.permission(), phone.load_config())
        with self.transcript.open('a') as stream:
            stream.write(json.dumps({'type': 'turn_context', 'payload': {
                'approvals_reviewer': 'auto_review'}}) + '\n')
        self.input_worker(launch.call_args)
        self.assertEqual(self.requests(), [])
        with self.transcript.open('a') as stream:
            stream.write(json.dumps({'type': 'turn_context', 'payload': {
                'approvals_reviewer': 'user', 'approval_policy': 'on-request'}}) + '\n')
        self.assertFalse(phone.automatic_approval(str(self.transcript)))
        with self.transcript.open('a') as stream:
            stream.write(json.dumps({'type': 'turn_context', 'payload': {
                'approvals_reviewer': 'user', 'approval_policy': 'never'}}) + '\n')
        self.assertTrue(phone.automatic_approval(str(self.transcript)))

    def test_same_permission_event_delivers_only_once_and_new_request_can_notify(self):
        self.config.write_text('NTFY_TOPIC=t\nINPUT_NOTIFY_SECONDS=0\n')
        with patch.object(phone, 'launch_input_worker') as launch:
            for _ in range(4):
                phone.handle(self.permission(), phone.load_config())
            self.assertEqual(launch.call_count, 1)
            self.input_worker(launch.call_args)
            self.input_worker(launch.call_args)
            phone.handle(self.permission(), phone.load_config())
            self.assertEqual(launch.call_count, 1)
            phone.handle(self.permission(command='DIFFERENT PRIVATE COMMAND'), phone.load_config())
            self.assertEqual(launch.call_count, 2)
            self.input_worker(launch.call_args)
        self.assertEqual(len(self.requests()), 2)
        self.assertNotIn('PRIVATE COMMAND', self.log.read_text())
        self.assertNotIn('PRIVATE COMMAND', (phone.state_directory('test-session') / 'state.json').read_text())

    def test_permission_cancels_on_stop_interrupt_or_human_reply(self):
        for kind in ['Stop', 'Interrupt', 'SessionEnd', 'UserPromptSubmit']:
            with self.subTest(kind=kind), patch.object(phone, 'launch_input_worker') as launch:
                phone.handle(self.permission(turn=kind), phone.load_config())
                phone.handle(self.event(kind, prompt='human reply'), phone.load_config())
                self.input_worker(launch.call_args)
        self.assertEqual(self.requests(), [])

    def test_permission_delay_is_observed(self):
        self.config.write_text('NTFY_TOPIC=t\nINPUT_NOTIFY_SECONDS=30\n')
        with patch.object(phone, 'launch_input_worker') as launch:
            phone.handle(self.permission(), phone.load_config())
        now = time.time()
        with patch.object(phone, 'title_for', return_value='Codex · test'), patch.object(phone, 'send') as send, patch.object(phone.time, 'time', side_effect=[now, now + 31]), patch.object(phone.time, 'sleep') as sleep:
            self.input_worker(launch.call_args)
        self.assertEqual(sleep.call_count, 1)
        self.assertEqual(send.call_args[0][-1], phone.PERMISSION)

    def test_duplicate_question_hook_only_notifies_once_per_call(self):
        event = self.event('PreToolUse', turn_id='turn-1', tool_name='request_user_input',
                           tool_use_id='question-1', tool_input={'questions': ['PRIVATE']})
        with patch.object(phone, 'title_for', return_value='Codex · test'), patch.object(phone.subprocess, 'Popen') as spawn:
            phone.handle(event, phone.load_config())
            phone.handle(event, phone.load_config())
            self.assertEqual(spawn.call_count, 1)
            event['tool_use_id'] = 'question-2'
            phone.handle(event, phone.load_config())
            self.assertEqual(spawn.call_count, 2)

    def test_replayed_human_prompt_with_same_turn_does_not_reset_notification(self):
        self.prompt(turn_id='human-turn')
        self.deliver(self.stop())
        self.prompt(turn_id='human-turn')
        self.deliver(self.stop())
        self.assertEqual(len(self.requests()), 1)
        self.prompt(turn_id='another-human-turn')
        self.deliver(self.stop())
        self.assertEqual(len(self.requests()), 2)

    def test_actual_detached_stop_hook_returns_promptly_and_worker_delivers(self):
        self.prompt()
        self.config.write_text('NTFY_TOPIC=test-topic\nIDLE_NOTIFY_SECONDS=0.15\n')
        start = time.monotonic()
        result = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(self.event('Stop')),
                                text=True, capture_output=True, timeout=2)
        self.assertEqual(result.stdout.strip(), '{}')
        self.assertLess(time.monotonic() - start, 1)
        deadline = time.monotonic() + 3
        while not self.log.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(self.requests()), 1)

    def test_session_title_index_does_not_use_prompt_content(self):
        directory = self.home / '.codex'
        directory.mkdir()
        (directory / 'session_index.jsonl').write_text(
            '{"id":"other","thread_name":"Wrong title"}\n'
            '{"id":"test-session","thread_name":"Old title"}\n'
            '{"id":"test-session","thread_name":"Моя сессия"}\n')
        self.assertEqual(phone.session_title('test-session'), 'Моя сессия')

    def test_delay_is_observed_and_background_task_completion_releases_wait(self):
        self.prompt()
        self.config.write_text('NTFY_TOPIC=t\nIDLE_NOTIFY_SECONDS=0.1\nBACKGROUND_WAIT_LIMIT_SECONDS=10\n')
        phone.handle(self.event('PreToolUse', tool_name='Bash', tool_use_id='job',
                               tool_input={'command': 'sleep 2'}), phone.load_config())
        token = self.stop()
        now = time.time()
        def finish_task(delay):
            self.assertGreater(delay, 0)
            phone.handle(self.event('PostToolUse', tool_use_id='job'), phone.load_config())
        with patch.object(phone, 'title_for', return_value='Codex · test'), patch.object(phone, 'send') as send, patch.object(phone.time, 'time', side_effect=[now, now + 1]), patch.object(phone.time, 'sleep', side_effect=finish_task) as sleep:
            self.deliver(token)
        self.assertEqual(sleep.call_count, 1)
        self.assertEqual(send.call_args[0][-1], phone.WAITING)

    def test_two_sessions_are_isolated_and_state_is_private(self):
        self.prompt()
        old = self.stop()
        other = self.event('UserPromptSubmit', prompt='Other session prompt')
        other['session_id'] = '../other-session'
        phone.handle(other, phone.load_config())
        self.deliver(old)
        self.assertEqual(len(self.requests()), 1)
        directory = phone.state_directory('test-session')
        self.assertEqual((directory / 'state.json').stat().st_mode & 0o777, 0o600)
        self.assertNotEqual(phone.state_directory('../other-session'), directory)
        self.assertNotIn('Other session prompt', (phone.state_directory('../other-session') / 'state.json').read_text())

    def test_repository_worktree_and_plain_folder_names(self):
        repo = self.home / 'repository'
        repo.mkdir()
        def git(*args):
            subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)
        git('init', '-q')
        git('-c', 'user.name=test', '-c', 'user.email=test@example.test', 'commit', '-q', '--allow-empty', '-m', 'initial')
        nested = repo / 'src'
        nested.mkdir()
        self.assertEqual(phone.project_name(str(nested)), 'repository')
        worktree = self.home / 'feature'
        git('worktree', 'add', '-q', str(worktree))
        self.assertEqual(phone.project_name(str(worktree)), 'repository (feature)')
        self.assertEqual(phone.project_name(str(self.home / 'plain')), 'plain')

    def test_old_tasks_do_not_hold_a_new_human_turn(self):
        self.prompt()
        phone.handle(self.event('PreToolUse', tool_name='Bash', tool_use_id='old',
                               tool_input={'command': 'sleep 20'}), phone.load_config())
        self.prompt('new turn')
        self.deliver(self.stop())
        self.assertEqual(json.loads(self.requests()[-1][-2])['message'], phone.WAITING)


if __name__ == '__main__':
    unittest.main()
