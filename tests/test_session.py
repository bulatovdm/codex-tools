import importlib.util
import json
import pathlib
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('session_hook', pathlib.Path(__file__).parents[1] / 'scripts/session-hook.py')
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)

class SessionTests(unittest.TestCase):
    def test_run_ids_keep_two_sessions_separate_and_reject_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for run, session in [('a' * 32, 'first'), ('b' * 32, 'second')]:
                (root / run).mkdir()
                hook.capture({'session_id': session, 'cwd': '/fixture'}, run, root)
            self.assertEqual(json.loads((root / ('a' * 32) / 'session.json').read_text())['session_id'], 'first')
            self.assertEqual(json.loads((root / ('b' * 32) / 'session.json').read_text())['session_id'], 'second')
            hook.capture({'session_id': 'wrong'}, '../outside', root)
            self.assertFalse((root.parent / 'outside').exists())

    def test_ordinary_codex_session_creates_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            hook.capture({'session_id': 'ordinary'}, '', root)
            self.assertEqual(list(root.iterdir()), [])

if __name__ == '__main__':
    unittest.main()
