import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone


SCRIPTS = pathlib.Path(__file__).parents[1] / 'scripts'
ROOT = '00000000-0000-0000-0000-000000000001'
CHILD = '00000000-0000-0000-0000-000000000002'


def event(kind, payload, at=None):
    record = {'type': kind, 'payload': payload}
    if at is not None:
        record['timestamp'] = datetime.fromtimestamp(at, timezone.utc).isoformat()
    return json.dumps(record) + '\n'


def metadata(model, fork=False):
    return event('session_meta', {
        'model_provider': 'openai',
        'forked_from_id': ROOT if fork else None,
        'timestamp': '2026-11-18T00:00:00Z',
    }) + event('turn_context', {'model': model})


def usage(last, total=None, at=None):
    fields = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens', 'output_tokens')
    return event('event_msg', {'type': 'token_count', 'info': {
        'last_token_usage': dict(zip(fields, last)),
        'total_token_usage': dict(zip(fields, total if total is not None else last)),
    }}, at)


class SessionUsageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = pathlib.Path(temporary.name)
        self.directory = self.home / '.codex'
        self.directory.mkdir()
        self.rollout = self.directory / 'main.jsonl'
        self.child = self.directory / 'child.jsonl'
        with sqlite3.connect(self.directory / 'state_5.sqlite') as db:
            db.execute('CREATE TABLE threads (id TEXT, rollout_path TEXT)')
            db.execute('INSERT INTO threads VALUES (?, ?)', (ROOT, str(self.rollout)))
            db.execute('CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT, status TEXT)')
        with sqlite3.connect(self.directory / 'thread_history_1.sqlite') as db:
            db.execute('CREATE TABLE thread_turns (thread_id TEXT, status TEXT, started_at INTEGER, completed_at INTEGER, duration_ms INTEGER)')
            db.execute('INSERT INTO thread_turns VALUES (?, ?, ?, ?, ?)', (ROOT, 'completed', 100, 120, 20000))

    def status(self, *args):
        env = {**os.environ, 'HOME': str(self.home), 'CODEX_HOME': str(self.directory)}
        return subprocess.run([sys.executable, str(SCRIPTS / 'status.py'), *args],
                              env=env, capture_output=True, text=True, check=True).stdout

    def report(self, session=ROOT):
        return json.loads(self.status('--session', session, '--usage', '--json'))

    def test_family_uses_request_model_cache_and_long_context_rates(self):
        first = usage((4000, 2000, 1000, 200))
        second = usage((300000, 250000, 0, 400), (304000, 252000, 1000, 600))
        self.rollout.write_text(metadata('gpt-6.2-sol') + first + first
                                + event('turn_context', {'model': 'gpt-6.2-astra'}) + second)
        self.child.write_text(metadata('gpt-6.1-sol') + usage((3000, 2000, 0, 100)))
        with sqlite3.connect(self.directory / 'state_5.sqlite') as db:
            db.execute('INSERT INTO threads VALUES (?, ?)', (CHILD, str(self.child)))
            db.execute('INSERT INTO thread_spawn_edges VALUES (?, ?, ?)', (ROOT, CHILD, 'closed'))
        with sqlite3.connect(self.directory / 'thread_history_1.sqlite') as db:
            db.execute('INSERT INTO thread_turns VALUES (?, ?, ?, ?, ?)', (CHILD, 'completed', 110, 130, 20000))
        report = self.report(CHILD)
        self.assertEqual(report['session_id'], ROOT)
        self.assertEqual(report['total_tokens'], 307700)
        self.assertEqual(report['api_priced_tokens'], 307700)
        self.assertAlmostEqual(report['api_standard_usd'], .0067 + 1.024 + .0032)
        self.assertAlmostEqual(report['output_tokens_per_work_second'], 700 / 30)
        row = self.status('--session', ROOT, '--agents', '--agent-row', '0', '--width', '100')
        self.assertIn('Токены: 307,7K (вход 307K, из кэша 254K, выход 700)', row)
        self.assertIn('Темп хода: —', row)
        self.assertIn('API Std ≈ $1.03', row)

    def test_resume_skips_inherited_fork_totals_and_finishes_partial_append(self):
        first = usage((100, 25, 0, 50), (100100, 25025, 0, 10050))
        second = usage((200, 100, 0, 25), (100300, 25125, 0, 10075))
        self.rollout.write_text(metadata('gpt-6.2-sol', fork=True) + first + second[:-5])
        self.assertEqual(self.report()['total_tokens'], 150)
        with self.rollout.open('a') as output:
            output.write(second[-5:])
        self.assertEqual(self.report()['total_tokens'], 375)
        self.assertEqual(self.report()['total_tokens'], 375)

    def test_rewritten_file_replaces_counters_and_marks_unpriced_usage(self):
        self.rollout.write_text(metadata('gpt-6.2-sol') + usage((4000, 1000, 0, 10)))
        self.assertEqual(self.report()['total_tokens'], 4010)
        self.rollout.write_text(metadata('other-model') + usage((10, 0, 0, 1))
                                + event('response_item', {'text': 'fixture output ' * 200}))
        report = self.report()
        self.assertEqual(report['total_tokens'], 11)
        self.assertIsNone(report['api_standard_usd'])

    def test_pace_resets_on_switch_excludes_inflight_batch_and_freezes_at_completion(self):
        now = int(time.time())
        started = now - 120
        run = 'a' * 32
        installed = self.home / '.local/share/codex-tools'
        state = installed / 'runs' / run
        state.mkdir(parents=True)
        launch = state / 'launch.json'
        launch.write_text(json.dumps({'cwd': str(self.home), 'environment': {}}))
        os.utime(launch, (started - 1, started - 1))
        (state / 'session.json').write_text(json.dumps({'session_id': ROOT}))
        tmux = installed / 'tmux/bin/tmux'
        tmux.parent.mkdir(parents=True)
        tmux.write_text('#!/bin/sh\ncase "$*" in *list-panes*) echo 0 ;; esac\n')
        tmux.chmod(0o700)

        def lifecycle(kind, seconds, **payload):
            return event('event_msg', {'type': kind, **payload}, started + seconds)

        def mode(tier, seconds):
            return lifecycle('thread_settings_applied', seconds,
                             thread_settings={'service_tier': tier})

        def append(records):
            with self.rollout.open('a') as output:
                output.write(records)

        old = (100, 0, 0, 600)
        inflight = usage((100, 0, 0, 200), (300, 0, 0, 1000), started + 30)
        self.rollout.write_text(metadata('gpt-6.2-sol') + usage(old)
                                + mode('default', 0) + lifecycle('task_started', 0, turn_id='new')
                                + usage((100, 0, 0, 200), (200, 0, 0, 800), started + 10)
                                + mode('priority', 20)
                                + inflight + inflight)
        args = ('--run', run)
        self.assertIn('Темп хода (Fast): замер…', self.status(*args, '--agents', '--agent-row', '0'))

        append(mode('priority', 32)
               + usage((100, 0, 0, 300), (400, 0, 0, 1300), started + 40)
               + lifecycle('task_complete', 45, turn_id='new'))
        report = self.report()
        self.assertEqual(report['output_tokens'], 1300)
        self.assertEqual(report['turn_pace'], {
            'tier': 'Fast', 'state': 'completed', 'output_tokens_per_second': 20,
        })
        row = self.status(*args, '--agents', '--agent-row', '0')
        self.assertIn('Прошлый ход (Fast): 20,0 ток/с', row)

        append(mode('ultrafast', 60) + lifecycle('task_started', 65, turn_id='next'))
        self.assertIn('Темп хода (Ultrafast): замер…', self.status(*args, '--agents', '--agent-row', '0'))
        self.assertEqual(self.report()['turn_pace']['state'], 'idle')


if __name__ == '__main__':
    unittest.main()
