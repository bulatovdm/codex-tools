import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
SESSION = '01a13b63-241d-7723-a926-9aeed94857ac'
RUN = 'a' * 32

class Terminal(io.StringIO):
    def isatty(self):
        return True

class ResumeRunsTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('resume_launcher', ROOT / 'scripts/codex-tools.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / '.local/bin').mkdir(parents=True)
        (self.home / '.local/bin/codex').touch()
        self.directory = self.home / '.local/share/codex-tools/runs' / RUN
        self.directory.mkdir(parents=True)
        self.write_run(self.directory)
        self.attached = 0
        self.dead = 0
        self.live = True
        self.start_override = None
        self.queried = []
        self.calls = []

    def write_run(self, directory, session=SESSION, cwd=None):
        (directory / 'session.json').write_text(json.dumps({'session_id':session, 'cwd':str(cwd or self.home)}))
        (directory / 'launch.json').write_text(json.dumps({'cwd':str(cwd or self.home), 'cli':str(self.home / '.local/bin/codex'), 'args':['resume'], 'environment':{}}))

    def command(self, args, **kwargs):
        self.queried.append(args)
        if 'new-session' in args:
            raise AssertionError('must reuse live run instead of starting a second Codex')
        run = args[args.index('-L') + 1].removeprefix('codex-tools-')
        directory = self.directory.parent / run
        import shlex
        start = shlex.join(['/usr/bin/python3', str(ROOT / 'scripts/launch.py'), str(directory)])
        return subprocess.CompletedProcess(args, 0 if self.live else 1,
            f'{self.attached}\t{self.dead}\t{self.start_override or start}\n', '')

    def invoke(self, args, choice=''):
        output = Terminal()
        with patch.object(Path, 'home', return_value=self.home), patch.object(self.module.os, 'getcwd', return_value=str(self.home)), patch.object(self.module, 'tmux_path', return_value='/fixture/tmux'), patch.object(sys, 'stdin', Terminal(choice)), contextlib.redirect_stdout(output), patch.object(self.module.subprocess, 'run', side_effect=self.command), patch.object(self.module.subprocess, 'call', side_effect=lambda args: self.calls.append(args) or 0):
            result = self.module.run(args)
        return result, output.getvalue()

    def test_exact_resume_reuses_detached_run_without_new_codex(self):
        self.assertEqual(self.invoke(['resume', SESSION])[0], 0)
        self.assertEqual(self.calls, [['/fixture/tmux', '-L', 'codex-tools-' + RUN, '-f', '/dev/null', 'attach-session', '-t', 'codex']])
        self.assertEqual(len(list(self.directory.parent.iterdir())), 1)

    def test_bare_resume_offers_live_project_runs_and_attaches_selection(self):
        result, output = self.invoke(['resume'], '1\n')
        self.assertEqual(result, 0)
        self.assertIn(SESSION, output)
        self.assertIn('0', output)
        self.assertEqual(len(self.calls), 1)

    def test_attached_run_reports_conflict_without_stealing_terminal(self):
        self.attached = 1
        result, output = self.invoke(['resume', SESSION])
        self.assertNotEqual(result, 0)
        self.assertIn('терминал', output)
        self.assertEqual(self.calls, [])

    def test_duplicate_exact_runs_fail_without_guessing(self):
        other = self.directory.parent / ('b' * 32)
        other.mkdir(); self.write_run(other)
        result, output = self.invoke(['resume', SESSION])
        self.assertNotEqual(result, 0)
        self.assertEqual(self.calls, [])

    def test_eof_cancels_picker_without_launch(self):
        self.assertEqual(self.invoke(['resume'])[0], 0)
        self.assertEqual(self.calls, [])

    def test_dead_finished_corrupt_foreign_and_symlink_runs_are_not_candidates(self):
        # Test discovery separately so fallback still belongs to the native launcher.
        for kind in ('dead', 'finished', 'corrupt', 'foreign', 'symlink', 'unavailable'):
            with self.subTest(kind=kind):
                self.dead = int(kind == 'dead'); self.live = kind != 'unavailable'
                self.write_run(self.directory)
                (self.directory / 'exit-code').unlink(missing_ok=True)
                if kind == 'finished': (self.directory / 'exit-code').write_text('0')
                if kind == 'corrupt': (self.directory / 'session.json').write_text('{}')
                if kind == 'foreign': self.write_run(self.directory, cwd=self.home / 'other')
                if kind == 'symlink':
                    data = (self.directory / 'session.json').read_text()
                    (self.home / 'outside.json').write_text(data)
                    (self.directory / 'session.json').unlink()
                    (self.directory / 'session.json').symlink_to(self.home / 'outside.json')
                with patch.object(Path, 'home', return_value=self.home), patch.object(self.module.subprocess, 'run', side_effect=self.command):
                    self.assertEqual(self.module.live_runs('/fixture/tmux', str(self.home)), [])
                if kind == 'symlink': (self.directory / 'session.json').unlink()

    def test_tmux_shell_command_outer_quotes_still_identify_own_launcher(self):
        import shlex
        command = shlex.join(['/usr/bin/python3', str(ROOT / 'scripts/launch.py'), str(self.directory)])
        self.start_override = '"' + command + '"'
        self.assertEqual(self.invoke(['resume', SESSION])[0], 0)
        self.assertEqual(len(self.calls), 1)

    def test_unrelated_pane_with_matching_socket_is_rejected(self):
        self.start_override = '/usr/bin/python3 /unrelated/launch.py'
        with patch.object(Path, 'home', return_value=self.home), patch.object(self.module.subprocess, 'run', side_effect=self.command):
            self.assertEqual(self.module.live_runs('/fixture/tmux', str(self.home)), [])

    def test_attachment_race_does_not_attach_or_start_another_codex(self):
        original = self.command
        calls = []
        def changing(args, **kwargs):
            calls.append(args)
            if len(calls) == 2:
                self.attached = 1
            return original(args, **kwargs)
        with patch.object(self, 'command', side_effect=changing):
            result, output = self.invoke(['resume', SESSION])
        self.assertNotEqual(result, 0)
        self.assertEqual(self.calls, [])

    def test_zero_preserves_native_history_picker(self):
        with patch.object(Path, 'home', return_value=self.home), patch.object(self.module.subprocess, 'run', side_effect=self.command), patch.object(sys, 'stdin', Terminal('0\n')), contextlib.redirect_stdout(Terminal()):
            self.assertIsNone(self.module.reuse_resume('/fixture/tmux', ['resume'], str(self.home)))
        self.assertEqual(self.calls, [])

    def test_other_modes_and_unknown_arguments_keep_native_behavior(self):
        for args in ([], ['fork', SESSION], ['resume', '--future'], ['resume', '--remote', 'host', SESSION], ['resume', '--worktree', SESSION], ['resume', '-m']):
            with self.subTest(args=args), patch.object(self.module, 'live_runs') as discover:
                self.assertIsNone(self.module.reuse_resume('/fixture/tmux', args, str(self.home)))
                discover.assert_not_called()

    def test_resume_selectors_and_cd_reuse_live_run(self):
        for args, choice in ((['resume', '--last'], '1\n'), (['resume', '--all'], '1\n'),
                             (['-C', '/tmp', 'resume', SESSION], ''),
                             (['resume', SESSION, '--cd=/tmp'], ''),
                             (['resume', SESSION, '--no-alt-screen'], '')):
            with self.subTest(args=args):
                self.calls.clear()
                self.assertEqual(self.invoke(args, choice)[0], 0)
                self.assertEqual(len(self.calls), 1)

    def test_live_resume_never_discards_overrides_or_prompt(self):
        for args, choice in ((['resume', SESSION, '-m', 'other'], ''),
                             (['resume', SESSION, 'continue please'], ''),
                             (['-c', 'x=true', 'resume', SESSION], ''),
                             (['resume', '--last', '--profile', 'work'], '1\n')):
            with self.subTest(args=args):
                self.calls.clear()
                result, output = self.invoke(args, choice)
                self.assertEqual(result, 1)
                self.assertEqual(self.calls, [])
                self.assertIn('cx resume ' + SESSION, output)

    def test_native_last_and_overrides_are_preserved_when_user_selects_zero(self):
        args = ['resume', '--last', '-m', 'other']
        candidate = {'directory': self.directory, 'session_id': SESSION, 'cwd': str(self.home), 'attached': 0}
        with patch.object(self.module, 'live_runs', return_value=[candidate]), patch.object(sys, 'stdin', Terminal('0\n')), contextlib.redirect_stdout(Terminal()):
            self.assertIsNone(self.module.reuse_resume('/fixture/tmux', args, str(self.home)))
        self.assertEqual(args, ['resume', '--last', '-m', 'other'])

    def test_subdirectory_is_same_project_and_all_includes_other_project(self):
        (self.home / '.git').mkdir()
        sub = self.home / 'src'; sub.mkdir()
        with patch.object(Path, 'home', return_value=self.home), patch.object(self.module.subprocess, 'run', side_effect=self.command):
            self.assertEqual(len(self.module.live_runs('/fixture/tmux', str(sub))), 1)
        self.write_run(self.directory, cwd=Path('/private/tmp/another-codex-project'))
        self.assertEqual(self.invoke(['resume', '--all'], '1\n')[0], 0)
        self.assertEqual(len(self.calls), 1)

    def test_cd_selects_project_before_picker(self):
        other = self.home / 'elsewhere'; other.mkdir()
        self.write_run(self.directory, cwd=other)
        self.assertEqual(self.invoke(['-C', str(other), 'resume'], '1\n')[0], 0)
        self.assertEqual(len(self.calls), 1)

    def test_named_resume_uses_title_only_for_verified_live_identity(self):
        index = self.home / '.codex/session_index.jsonl'
        index.parent.mkdir()
        index.write_text('[]\nnot json\n{"id": []}\n' + json.dumps({'id': SESSION, 'thread_name': 'my conversation'}) + '\n')
        with patch.dict(self.module.os.environ, {'CODEX_HOME':str(index.parent)}):
            self.assertEqual(self.invoke(['resume', 'my conversation'])[0], 0)
        self.assertEqual(len(self.calls), 1)

    def test_named_resume_refuses_ambiguous_titles(self):
        other_id = '01a13b63-241d-7723-a926-9aeed94857ad'
        other = self.directory.parent / ('b' * 32)
        other.mkdir(); self.write_run(other, session=other_id)
        index = self.home / '.codex/session_index.jsonl'
        index.parent.mkdir()
        index.write_text(''.join(json.dumps({'id': sid, 'thread_name': 'same'}) + '\n' for sid in (SESSION, other_id)))
        with patch.dict(self.module.os.environ, {'CODEX_HOME':str(index.parent)}):
            result, output = self.invoke(['resume', 'same'])
        self.assertEqual(result, 1)
        self.assertEqual(self.calls, [])

    def test_unmatched_id_and_finished_run_defer_to_native_resume(self):
        for finished in (False, True):
            if finished:
                (self.directory / 'exit-code').write_text('0')
            target = SESSION if finished else '01a13b63-241d-7723-a926-9aeed94857ad'
            with patch.object(Path, 'home', return_value=self.home), patch.object(self.module.subprocess, 'run', side_effect=self.command):
                self.assertIsNone(self.module.reuse_resume('/fixture/tmux', ['resume', target], str(self.home)))
        self.assertEqual(self.calls, [])

    def test_ctrl_c_cancels_resume_selection(self):
        with patch('builtins.input', side_effect=KeyboardInterrupt):
            self.assertEqual(self.invoke(['resume'])[0], 0)
        self.assertEqual(self.calls, [])

    def test_reattach_applies_lifecycle_before_attachment(self):
        self.invoke(['resume', SESSION])
        hook = next(args for args in self.queried if 'set-hook' in args)
        self.assertIn('set-option -t codex destroy-unattached on', hook)
        binding = next(args for args in self.queried if 'bind-key' in args)
        self.assertEqual(binding[-7:], ['set-option', '-t', 'codex', 'destroy-unattached', 'off', '\\;', 'detach-client'])

    def test_finished_reattach_cleans_success_and_preserves_failure_diagnostics(self):
        for status in (0, 2, 130):
            with self.subTest(status=status):
                self.directory.mkdir(exist_ok=True)
                (self.directory / 'exit-code').write_text(str(status))
                with contextlib.redirect_stdout(Terminal()):
                    self.assertEqual(self.module.finish_attachment(self.directory, 0, '/fixture/tmux'), status)
                self.assertEqual(self.directory.exists(), status != 0)

    def test_detachment_reports_exact_return_command(self):
        result, output = self.invoke(['resume', SESSION])
        self.assertEqual(result, 0)
        self.assertIn('cx resume ' + SESSION, output)

    def test_changed_or_finished_run_during_selection_is_refused(self):
        original = self.module.read_run
        count = [0]
        def changing(directory):
            count[0] += 1
            if count[0] == 2:
                (directory / 'exit-code').write_text('0')
            return original(directory)
        with patch.object(self.module, 'read_run', side_effect=changing):
            result, output = self.invoke(['resume'], '1\n')
        self.assertEqual(result, 1)
        self.assertEqual(self.calls, [])

if __name__ == '__main__':
    unittest.main()
