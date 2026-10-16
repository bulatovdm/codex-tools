import importlib.util
import pathlib
import stat
import sys
import tempfile
import unittest

if sys.version_info >= (3, 11):
    import tomllib
    spec = importlib.util.spec_from_file_location('history', pathlib.Path(__file__).parents[1] / 'scripts/history.py')
    history = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(history)


@unittest.skipIf(sys.version_info < (3, 11), 'history tool requires Python 3.11+')
class HistoryTests(unittest.TestCase):
    def test_preserves_unrelated_config_and_updates_profiles(self):
        original = ('# personal settings\nmodel = "test"\n[history]\n'
                    'persistence = "none" # old\nmax_bytes = 100\n'
                    '[mcp_servers.example]\ncommand = "example"\n'
                    '[profiles."my profile".history]\nmax_bytes = 10\n'
                    '[profiles.other]\nmodel = "other"\n')
        result = history.keep_all(original)
        parsed = tomllib.loads(result)
        self.assertEqual(parsed['history'], {'persistence': 'save-all'})
        self.assertEqual(parsed['profiles']['my profile']['history'], {'persistence': 'save-all'})
        self.assertEqual(parsed['mcp_servers'], tomllib.loads(original)['mcp_servers'])
        self.assertIn('# personal settings\nmodel = "test"', result)
        self.assertIn('[profiles.other]\nmodel = "other"\n', result)
        self.assertEqual(history.keep_all(result), result)

    def test_missing_table_and_no_final_newline(self):
        result = history.keep_all('model = "test"')
        self.assertEqual(tomllib.loads(result), {'model': 'test', 'history': {'persistence': 'save-all'}})

    def test_unsupported_syntax_and_invalid_toml_leave_file_untouched(self):
        for original in ['history = { max_bytes = 100 }\n',
                         'history.max_bytes = 100\n',
                         'value = """\n[history]\nmax_bytes = 10\n"""\n',
                         '[history]\ninvalid']:
            with self.subTest(original=original), tempfile.TemporaryDirectory() as directory:
                path = pathlib.Path(directory) / 'config.toml'
                path.write_text(original)
                with self.assertRaises(ValueError):
                    history.apply(path)
                self.assertEqual(path.read_text(), original)
                self.assertEqual(list(path.parent.iterdir()), [path])

    def test_private_backup_mode_and_idempotence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'config.toml'
            original = b'[history]\r\nmax_bytes = 100\r\n'
            path.write_bytes(original)
            path.chmod(0o640)
            backup = history.apply(path)
            self.assertEqual(backup.read_bytes(), original)
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
            self.assertIn(b'\r\n', path.read_bytes())
            self.assertIsNone(history.apply(path))
            self.assertEqual(len(list(path.parent.iterdir())), 2)

    def test_new_config_and_symlink_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'new' / 'config.toml'
            history.apply(path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            link = path.parent / 'link.toml'
            link.symlink_to(path)
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                history.apply(link)
            self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
