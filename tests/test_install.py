import importlib.util
import json
import pathlib
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('installer', pathlib.Path(__file__).parents[1] / 'scripts/install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

class InstallTests(unittest.TestCase):
    def test_keeps_other_hooks_shell_lines_and_user_customization(self):
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory)
            (home / '.codex').mkdir()
            other = {'hooks': {'SessionStart': [{'hooks': [{'type': 'command', 'command': 'echo original'}]}]}}
            (home / '.codex/hooks.json').write_text(json.dumps(other))
            (home / '.zshrc').write_text('export ORIGINAL=value\n')
            installer.install_files(home, pathlib.Path(__file__).parents[1])
            customized = home / '.config/codex-tools/status.json'
            customized.write_text('{"fields":["project"]}')
            installer.install_files(home, pathlib.Path(__file__).parents[1])
            hooks = json.loads((home / '.codex/hooks.json').read_text())
            self.assertEqual(hooks['hooks']['SessionStart'][0], other['hooks']['SessionStart'][0])
            self.assertEqual(len(hooks['hooks']['SessionStart']), 3)
            self.assertIn('export ORIGINAL=value', (home / '.zshrc').read_text())
            self.assertEqual((home / '.zshrc').read_text().count('source "$HOME/.local/share/codex-browser-guard/shell.zsh"'), 1)
            self.assertEqual(customized.read_text(), '{"fields":["project"]}')
            self.assertEqual((home / '.local/bin/codex-tools').resolve().name, 'codex-tools.py')
            topic = (home / '.codex/notify.env').read_text()
            installer.install_files(home, pathlib.Path(__file__).parents[1])
            self.assertEqual((home / '.codex/notify.env').read_text(), topic)
            self.assertEqual((home / '.codex/notify.env').stat().st_mode & 0o777, 0o600)
            for event in ['Stop', 'PermissionRequest', 'PreToolUse', 'PostToolUse', 'Interrupt', 'SessionEnd']:
                self.assertEqual(len(hooks['hooks'][event]), 1)

    def test_reuses_claude_phone_config_without_copying_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory)
            (home / '.claude').mkdir()
            config = home / '.claude/notify.env'
            config.write_text('NTFY_TOPIC=shared-private-topic\n')
            installer.install_files(home, pathlib.Path(__file__).parents[1])
            self.assertFalse((home / '.codex/notify.env').exists())
            self.assertEqual(config.read_text(), 'NTFY_TOPIC=shared-private-topic\n')

if __name__ == '__main__':
    unittest.main()
