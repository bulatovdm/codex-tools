import importlib.util
import json
import pathlib
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('browser_guard', str(pathlib.Path(__file__).parents[1] / 'scripts/codex-browser-guard.py'))
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

class BrowserGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.user = self.root / 'user'
        self.user.mkdir()
        self.state = self.root / 'state'
        self.cli = self.root / 'codex'
        self.cli.write_text('#!/bin/sh\nexit 0\n')
        self.cli.chmod(0o755)
        self.config = self.user / '.codex/config.toml'
        self.config.parent.mkdir()
        self.config.write_text('[mcp_servers.other.env]\nCODEX_CLI_PATH = "leave-other-alone"\n[mcp_servers.node_repl.env]\nTOKEN = "fixture-secret"\nCODEX_CLI_PATH = "old-cli" # keep this comment\n[features]\nhooks = true\n')
        self.plugin = self.plugin_at('old-version')

    def tearDown(self):
        self.temp.cleanup()

    def plugin_at(self, version):
        path = self.user / '.codex/plugins/cache/openai-bundled/unified-computer-use' / version / '.mcp.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'mcpServers': {'cua_repl': {'command': 'node', 'args': ['unchanged'], 'env': {'CODEX_CLI_PATH': 'old-cli', 'ALLOW': 'unchanged'}}, 'other': {'env': {'CODEX_CLI_PATH': 'other-cli'}}}}))
        return path

    def run_guard(self):
        return guard.repair(self.user, self.cli, self.state)

    def test_repairs_only_selected_fields_and_is_idempotent(self):
        self.assertEqual(len(self.run_guard()), 2)
        text = self.config.read_text()
        self.assertIn('CODEX_CLI_PATH = "leave-other-alone"', text)
        self.assertIn('TOKEN = "fixture-secret"', text)
        self.assertIn('# keep this comment', text)
        data = json.loads(self.plugin.read_text())
        self.assertEqual(data['mcpServers']['cua_repl']['env']['CODEX_CLI_PATH'], str(self.cli))
        self.assertEqual(data['mcpServers']['other']['env']['CODEX_CLI_PATH'], 'other-cli')
        self.assertEqual(data['mcpServers']['cua_repl']['args'], ['unchanged'])
        stamp = self.plugin.stat().st_mtime_ns
        self.assertEqual(self.run_guard(), [])
        self.assertEqual(self.plugin.stat().st_mtime_ns, stamp)
        for path in (self.state / 'backups').iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_recovers_when_an_update_recreates_or_overwrites_plugin(self):
        self.run_guard()
        new = self.plugin_at('new-version')
        self.assertEqual(self.run_guard(), [str(new)])
        data = json.loads(new.read_text())
        data['mcpServers']['cua_repl']['env']['CODEX_CLI_PATH'] = 'bundled-old-again'
        new.write_text(json.dumps(data))
        self.assertEqual(self.run_guard(), [str(new)])

    def test_refuses_incompatible_cli_without_changing_configuration(self):
        self.cli.write_text('#!/bin/sh\necho TIOCSTI >&2\nexit 65\n')
        before = self.config.read_bytes(), self.plugin.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'TIOCSTI'):
            self.run_guard()
        self.assertEqual(before, (self.config.read_bytes(), self.plugin.read_bytes()))

    def test_reprobes_replaced_cli_and_preserves_incompatible_new_runtime(self):
        self.run_guard()
        self.cli.write_text('#!/bin/sh\necho new-runtime-failure >&2\nexit 65\n')
        new = self.plugin_at('updated')
        before = new.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'new-runtime-failure'):
            self.run_guard()
        self.assertEqual(before, new.read_bytes())

    def test_malformed_file_is_not_overwritten(self):
        self.plugin.write_text('{partial update')
        before = self.config.read_bytes()
        with self.assertRaises(ValueError):
            self.run_guard()
        self.assertEqual(self.plugin.read_text(), '{partial update')
        self.assertEqual(self.config.read_bytes(), before)

    def test_external_symlink_is_not_rewritten(self):
        other = self.root / 'other.json'
        original = self.plugin.read_bytes()
        other.write_bytes(original)
        self.plugin.unlink()
        self.plugin.symlink_to(other)
        with self.assertRaisesRegex(RuntimeError, 'symlink'):
            self.run_guard()
        self.assertEqual(other.read_bytes(), original)

if __name__ == '__main__':
    unittest.main()
