import importlib.util
import json
import os
import pathlib
import tempfile
import unittest
import contextlib
import io
from unittest import mock

spec = importlib.util.spec_from_file_location('status', pathlib.Path(__file__).parents[1] / 'scripts/status.py')
status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status)

class StatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.project = self.root / 'empirika'
        (self.project / 'devops/support').mkdir(parents=True)
        (self.project / 'devops/project.conf').write_text('PROJECT_NAME="empirika"\nPROD_DOMAIN="empirika.example"\nDEV_DOMAIN="dev.empirika.example"\n')
        (self.project / 'devops/support/listeners.sh').touch()
        self.listeners = self.root / 'listeners'
        self.listeners.mkdir()
        self.environment = {'EMPIRIKA_LISTENERS_DIR': str(self.listeners)}

    def tearDown(self):
        self.temp.cleanup()

    def listener(self, session, stand='prod', pid=None):
        pid = pid or os.getpid()
        (self.listeners / (str(pid) + '.stand')).write_text(stand)
        (self.listeners / (str(pid) + '.session')).write_text(session)
        (self.listeners / (str(pid) + '.filter')).write_text('empirika\nЛендинг\n')

    def test_uses_environment_not_dotenv_and_reads_actual_scope(self):
        (self.project / '.env').write_text('EMPIRIKA_BASE_URL=https://localhost\n')
        self.listener('ours')
        value = status.collect(self.project, 'ours', self.environment, self.root)
        self.assertEqual(value['environment'], 'бой')
        self.assertEqual(value['address'], 'empirika.example')
        self.assertEqual(value['listener'], 'слушатель шины жив')
        self.assertEqual(value['scope'], 'empirika · Лендинг')

    def test_does_not_claim_another_sessions_listener(self):
        self.listener('another')
        value = status.collect(self.project, 'ours', self.environment, self.root)
        self.assertEqual(value['listener'], 'слушателя шины нет')

    def test_does_not_claim_listener_without_known_session(self):
        self.listener('another')
        self.assertEqual(status.collect(self.project, '', self.environment, self.root)['listener'], 'сессия не определена')

    def test_dead_process_and_other_stand_are_not_alive(self):
        self.listener('ours', pid=99999999)
        self.listener('ours', stand='dev')
        self.assertEqual(status.collect(self.project, 'ours', self.environment, self.root)['listener'], 'слушателя шины нет')

    def test_dev_and_local_addresses(self):
        self.environment['EMPIRIKA_BASE_URL'] = 'https://dev.empirika.example/'
        self.assertEqual(status.collect(self.project, 'ours', self.environment, self.root)['environment'], 'тестовый')
        self.environment['EMPIRIKA_BASE_URL'] = 'https://secret:password@localhost:3720/path?token=hidden'
        value = status.collect(self.project, 'ours', self.environment, self.root)
        self.assertEqual(value['environment'], 'локальный')
        self.assertEqual(value['address'], 'localhost:3720')

    def test_custom_field_order_and_terminal_safety(self):
        settings = {'fields': ['address', 'environment'], 'separator': ' | '}
        value = {'address': 'safe\033]2;bad\007\n#[fg=red]', 'environment': 'бой'}
        rendered = status.render(value, settings, tmux=True)
        self.assertNotIn('\033', rendered)
        self.assertNotIn('\007', rendered)
        self.assertNotIn('\n', rendered)
        self.assertIn('##[', rendered)
        self.assertTrue(rendered.endswith(' | бой'))

    def test_generic_project_does_not_report_empirika(self):
        value = status.collect(self.root, '', {}, self.root)
        self.assertEqual(value['project'], self.root.name)
        self.assertNotIn('listener', value)

    def test_new_run_does_not_inherit_parent_codex_session(self):
        run = 'a' * 32
        directory = self.root / '.local/share/codex-tools/runs' / run
        directory.mkdir(parents=True)
        (directory / 'launch.json').write_text(json.dumps({'cwd': str(self.project), 'environment': self.environment}))
        self.listener('parent')
        output = io.StringIO()
        with mock.patch.object(status.pathlib.Path, 'home', return_value=self.root), mock.patch.dict(os.environ, {'CODEX_THREAD_ID': 'parent'}), mock.patch('sys.argv', ['status.py', '--run', run, '--json']), contextlib.redirect_stdout(output):
            status.main()
        self.assertEqual(json.loads(output.getvalue())['listener'], 'сессия не определена')

if __name__ == '__main__':
    unittest.main()
