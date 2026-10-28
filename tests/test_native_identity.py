import contextlib
import importlib.util
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
NATIVE = '01a1816e-e7ea-7211-abb7-060d02ff7822'

FAKE_APP_SERVER = r'''#!/usr/bin/python3
import json, os, sys, time
log = open(os.environ['FAKE_LOG'], 'a')
mode = os.environ.get('FAKE_MODE', 'ok')
assert sys.argv[1:] == ['app-server'], sys.argv
for line in sys.stdin:
    message = json.loads(line)
    log.write(json.dumps(message) + '\n'); log.flush()
    if 'id' not in message:
        continue
    method = message['method']
    if mode == 'hang' and method == 'thread/start':
        time.sleep(30)
    if mode == 'error' and method == 'thread/name/set':
        result = {'id': message['id'], 'error': {'code': -32600, 'message': 'fixture'}}
    elif method == 'thread/start':
        print(json.dumps({'method': 'thread/started', 'params': {}}), flush=True)
        result = {'id': message['id'], 'result': {'thread': {'id': os.environ['FAKE_ID']}}}
    else:
        result = {'id': message['id'], 'result': {}}
    print(json.dumps(result), flush=True)
'''


class Terminal(io.StringIO):
    def isatty(self):
        return True


def load():
    spec = importlib.util.spec_from_file_location('identity_launcher', ROOT / 'scripts/codex-tools.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NativeThreadTests(unittest.TestCase):
    def setUp(self):
        self.launcher = load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cli = self.root / 'codex'
        self.cli.write_text(FAKE_APP_SERVER)
        self.cli.chmod(0o700)
        self.log = self.root / 'requests.jsonl'

    def create(self, mode='ok', timeout=5):
        environment = {'FAKE_LOG': str(self.log), 'FAKE_MODE': mode, 'FAKE_ID': NATIVE}
        with patch.dict(os.environ, environment):
            return self.launcher.create_native_thread(str(self.cli), str(self.root), 'empirika 10-28 10:16', timeout)

    def requests(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_app_server_creates_a_persisted_named_thread_without_a_model_turn(self):
        self.assertEqual(self.create(), NATIVE)
        methods = [request['method'] for request in self.requests()]
        self.assertEqual(methods, ['initialize', 'initialized', 'thread/start', 'thread/name/set'])
        start, name = self.requests()[2]['params'], self.requests()[3]['params']
        self.assertEqual(start, {'cwd': str(self.root), 'sessionStartSource': 'startup'})
        # An unnamed thread is never written to disk, so Codex could not resume it.
        self.assertEqual(name, {'threadId': NATIVE, 'name': 'empirika 10-28 10:16'})
        self.assertNotIn('turn/start', methods)

    def test_failed_or_stalled_app_server_returns_no_identity(self):
        self.assertIsNone(self.create('error'))
        self.assertIsNone(self.create('hang', timeout=1))
        self.assertIsNone(self.launcher.create_native_thread(str(self.root / 'missing'), str(self.root), 'x', 1))


class StartupIdentityTests(unittest.TestCase):
    def setUp(self):
        self.launcher = load()

    def invoke(self, args, cwd, native=NATIVE):
        context = subprocess.CompletedProcess([], 0, json.dumps({'version': 1, 'enabled': True, 'project_root': str(cwd)}), '')
        with patch.object(self.launcher.shutil, 'which', return_value='/fixture/work-worker'), \
                patch.object(self.launcher.subprocess, 'run', return_value=context), \
                patch.object(self.launcher, 'create_native_thread', return_value=native) as create:
            return self.launcher.startup_arguments(args, str(cwd), '/fixture/codex'), create

    def test_connected_empty_launch_resumes_a_prepared_thread(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            sub = root / 'src'
            sub.mkdir()
            (args, native), create = self.invoke(['-C', str(sub), '-m', 'fixture-model'], root)
            self.assertEqual(native, NATIVE)
            self.assertEqual(args, ['resume', '-C', str(sub), '-m', 'fixture-model', NATIVE])
            self.assertNotIn(self.launcher.WORK_WORKER_STARTUP_PROMPT, args)
            cli, cwd, name, timeout = create.call_args.args
            self.assertEqual((cli, cwd), ('/fixture/codex', str(sub)))
            self.assertRegex(name, '^' + re.escape(root.name) + r' \d\d-\d\d \d\d:\d\d$')
            self.assertLessEqual(timeout, 10)

    def test_unavailable_app_server_keeps_the_service_turn_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            (args, native), _ = self.invoke([], root, native=None)
            self.assertIsNone(native)
            self.assertEqual(args, ['--', self.launcher.WORK_WORKER_STARTUP_PROMPT])


class RunIdentityTests(unittest.TestCase):
    def setUp(self):
        self.launcher = load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / '.local/bin').mkdir(parents=True)
        (self.home / '.local/bin/codex').touch()
        self.runs = self.home / '.local/share/codex-tools/runs'

    def saved(self, native, cwd=None):
        root = self.home / '.codex'
        root.mkdir(exist_ok=True)
        rollout = root / (native + '.jsonl')
        rollout.touch()
        with sqlite3.connect(root / 'state_5.sqlite') as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS threads (id TEXT, cwd TEXT, name TEXT, title TEXT, '
                               'recency_at_ms INTEGER, rollout_path TEXT, source TEXT, archived INTEGER)')
            connection.execute('INSERT INTO threads VALUES (?, ?, ?, ?, 1000, ?, ?, 0)',
                               (native, str(cwd or self.home), 'Разговор', '', str(rollout), 'cli'))

    def launch(self, args, startup=None, choice=''):
        observed = {}

        def command(argv, **kwargs):
            if 'list-panes' in argv or 'display-message' in argv:
                return subprocess.CompletedProcess(argv, 1, '', '')
            return subprocess.CompletedProcess(argv, 0, '', '')

        def attach(argv):
            run = next(self.runs.iterdir())
            observed['args'] = json.loads((run / 'launch.json').read_text())['args']
            path = run / 'session.json'
            observed['session'] = json.loads(path.read_text()) if path.exists() else None
            (run / 'exit-code').write_text('0')
            return 0

        startup = startup or (lambda arguments, cwd, cli=None: (arguments, None))
        with patch.object(Path, 'home', return_value=self.home), \
                patch.object(self.launcher.os, 'getcwd', return_value=str(self.home)), \
                patch.object(self.launcher, 'tmux_path', return_value='/fixture/tmux'), \
                patch.object(self.launcher, 'live_runs', return_value=[]), \
                patch.object(self.launcher, 'startup_arguments', side_effect=startup), \
                patch.object(sys, 'stdin', Terminal(choice)), contextlib.redirect_stdout(Terminal()), \
                patch.object(self.launcher.subprocess, 'run', side_effect=command), \
                patch.object(self.launcher.subprocess, 'call', side_effect=attach):
            self.assertEqual(self.launcher.run(args), 0)
        return observed

    def test_prepared_thread_identity_is_recorded_before_codex_starts(self):
        observed = self.launch([], startup=lambda arguments, cwd, cli=None: (['resume', NATIVE], NATIVE))
        self.assertEqual(observed['args'], ['resume', NATIVE])
        self.assertEqual(observed['session'], {'session_id': NATIVE, 'cwd': str(self.home)})

    def test_exact_saved_resume_is_known_before_the_first_turn(self):
        self.saved(NATIVE)
        observed = self.launch(['resume', NATIVE])
        self.assertEqual(observed['session'], {'session_id': NATIVE, 'cwd': str(self.home)})

    def test_saved_choice_from_the_codex_tools_list_is_known_before_the_first_turn(self):
        self.saved(NATIVE)
        observed = self.launch(['resume'], choice='1\n')
        self.assertEqual(observed['args'], ['resume', NATIVE])
        self.assertEqual(observed['session'], {'session_id': NATIVE, 'cwd': str(self.home)})

    def test_unknown_named_or_picker_resume_waits_for_the_trusted_hook(self):
        self.saved(NATIVE)
        for args in (['resume', '01a1816e-e7ea-7211-abb7-000000000000'], ['resume', 'Разговор'], ['fix this']):
            with self.subTest(args=args):
                for run in list(self.runs.iterdir()) if self.runs.exists() else []:
                    for item in run.iterdir():
                        item.unlink()
                    run.rmdir()
                self.assertIsNone(self.launch(args)['session'])


if __name__ == '__main__':
    unittest.main()
