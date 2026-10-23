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

    def test_non_plain_resume_and_overrides_keep_native_behavior(self):
        for args in ([], ['fork', SESSION], ['resume', '--last'], ['resume', SESSION, '-m', 'other'], ['-C', '/tmp', 'resume', SESSION]):
            with self.subTest(args=args), patch.object(self.module, 'live_runs') as discover:
                self.assertIsNone(self.module.reuse_resume('/fixture/tmux', args, str(self.home)))
                discover.assert_not_called()

if __name__ == '__main__':
    unittest.main()
