import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest


SCRIPTS = pathlib.Path(__file__).parents[1] / 'scripts'
ROOT = '00000000-0000-0000-0000-000000000001'
CHILD = '00000000-0000-0000-0000-000000000002'


def event(kind, payload):
    return json.dumps({'type': kind, 'payload': payload}) + '\n'


def metadata(model, fork=False):
    return event('session_meta', {
        'model_provider': 'openai',
        'forked_from_id': ROOT if fork else None,
        'timestamp': '2026-11-18T00:00:00Z',
    }) + event('turn_context', {'model': model})


def usage(last, total=None):
    fields = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens', 'output_tokens')
    return event('event_msg', {'type': 'token_count', 'info': {
        'last_token_usage': dict(zip(fields, last)),
        'total_token_usage': dict(zip(fields, total if total is not None else last)),
    }})


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
        self.assertIn('темп сессии: 23,3 ток/с', row)
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


if __name__ == '__main__':
    unittest.main()
