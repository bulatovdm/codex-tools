import importlib.util
import json
import os
import sqlite3
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

    def test_agents_resolve_main_and_distinguish_working_ready_and_closed(self):
        directory = self.root / '.codex'; directory.mkdir()
        with sqlite3.connect(directory / 'state_5.sqlite') as db:
            db.execute('CREATE TABLE threads (id TEXT, name TEXT, agent_nickname TEXT)')
            db.execute('CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT, status TEXT)')
            db.executemany('INSERT INTO threads VALUES (?,?,?)', [
                ('main','Мой основной поток',None), ('a','task','Pascal'),
                ('b','task','Ramanujan'), ('c','old','Fermat'), ('foreign','Чужой',None)])
            db.executemany('INSERT INTO thread_spawn_edges VALUES (?,?,?)', [
                ('main','a','open'), ('a','b','open'), ('main','c','closed')])
        with sqlite3.connect(directory / 'thread_history_1.sqlite') as db:
            db.execute('CREATE TABLE thread_turns (thread_id TEXT, status TEXT, rollout_ordinal INTEGER)')
            db.executemany('INSERT INTO thread_turns VALUES (?,?,?)', [
                ('a','completed',1), ('a','inProgress',3), ('b','completed',4), ('c','inProgress',9)])
        text = status.agent_overview('b', {}, self.root)
        self.assertIn('Основной: Мой основной поток', text)
        self.assertIn('Pascal — работает', text)
        self.assertIn('Ramanujan — готов', text)
        self.assertIn('закрыты: 1', text)
        self.assertNotIn('Fermat — работает', text)
        self.assertNotIn('Чужой', text)
        selected = status.agent_overview('main', {}, self.root, structured=True, selected='b', show_selection=True)
        self.assertIn(('▶ Ramanujan — готов', 'selected'), selected)
        self.assertFalse(any(text.startswith('▶ Основной') for text, _ in selected))
        unknown = status.agent_overview('main', {}, self.root, structured=True, selected='foreign', show_selection=True)
        self.assertTrue(any('Выбранный: неизвестен' in text for text, _ in unknown))
        closed = status.agent_overview('main', {}, self.root, structured=True, selected='c')
        self.assertIn(('▶ Fermat — закрыт', 'selected'), closed)
        self.assertEqual(closed[1], ('Pascal — работает', 'working'))
        (directory / 'thread_history_1.sqlite').unlink()
        self.assertIn('статус неизвестен', status.agent_overview('main', {}, self.root))

    def test_agent_data_unavailable_is_not_claimed_as_no_active_agents(self):
        self.assertEqual(status.agent_overview('', {}, self.root), '')
        self.assertEqual(status.agent_overview('missing', {}, self.root), 'Агенты: данные недоступны')

    def test_agent_rows_wrap_and_style_working_entries_only(self):
        entries = [('Основной: task', 'main'), ('Pascal — работает', True), ('Fermat — готов', False)]
        plain = status.agent_rows(entries, 35)
        self.assertGreater(len(plain), 1)
        self.assertTrue(all(status.cell_width(row) <= 35 for row in plain))
        styled = ''.join(status.agent_rows(entries, 35, tmux=True))
        self.assertIn('#[bg=colour236,fg=colour255,bold]Основной: task', styled)
        self.assertIn('#[bg=colour236,fg=colour117,bold]Pascal', styled)
        self.assertIn('#[bg=colour236,fg=colour255,nobold]Fermat', styled)

    def test_agent_wrapping_handles_wide_names_and_escapes_untrusted_markup(self):
        entries = [('中文名字' * 15 + '\033#[bg=red]', True)]
        plain = status.agent_rows(entries, 30)
        self.assertLessEqual(len(plain), 4)
        self.assertTrue(all(status.cell_width(row) <= 30 for row in plain))
        escaped = ''.join(status.agent_rows([('#[bg=red]', False)], 80, tmux=True))
        self.assertIn('##[bg=red]', escaped)

    def test_current_thread_is_read_only_from_last_native_footer(self):
        native = '01a1ca26-c037-78e3-b5fd-e84a1864161e'
        self.assertEqual(status.footer_thread_id('text\n  ' + native + ' · model\n'), native)
        self.assertIsNone(status.footer_thread_id(native + '\n› input\nmodel · context'))
        self.assertIsNone(status.footer_thread_id('› ' + native))
        self.assertEqual(status.footer_thread_id('  model · Context 47% left · ' + native + '\n  ← for agents'), native)
        partial = '01a1bfd0-20fa-7fa0-…'
        self.assertEqual(status.footer_thread_id('  model · Context 47% left · 475K window · ' + partial), partial)
        root = '01a1bfd0-20fa-7fa0-bb94-4234ce3209b2'
        self.assertEqual(status.resolve_visible_agent(partial, [root, native]), root)
        self.assertIsNone(status.resolve_visible_agent(partial, [root, root[:-1] + '3']))

    def test_selected_agent_style_is_distinct_from_working_state(self):
        text = ''.join(status.agent_rows([('▶ Pascal — работает', 'selected'), ('Ramanujan — работает', True)], 120, tmux=True))
        self.assertIn('#[bg=colour238,fg=colour229,bold]▶ Pascal', text)
        self.assertIn('#[bg=colour236,fg=colour117,bold]', text)

    def test_agent_controls_have_dedicated_last_row(self):
        rows = status.agent_panel_rows([('Основной: main', 'main'), ('▶ Pascal — работает', 'selected')], 160)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0], ' ' * 160)
        self.assertEqual(rows[2], ' ' * 160)
        self.assertTrue(rows[1].startswith('  Основной: main'))
        self.assertTrue(rows[1].endswith('  '))
        self.assertIn('▶ Pascal ●', rows[1])
        self.assertNotIn('работает', rows[1])
        self.assertNotIn('/subagents', ''.join(rows[:-1]))
        self.assertIn('/subagents', rows[-1])
        self.assertIn('Сейчас: Pascal', rows[-1])
        unknown = status.agent_panel_rows([('Основной: main', 'main')], 160)
        self.assertIn('Выбор: Codex', unknown[-1])
        self.assertNotIn('первым', unknown[-1])
        self.assertNotIn('Выбранный: неизвестен', ''.join(unknown))

    def test_working_agents_move_first_and_refresh_after_state_changes(self):
        directory = self.root / '.codex'; directory.mkdir()
        with sqlite3.connect(directory / 'state_5.sqlite') as db:
            db.execute('CREATE TABLE threads (id TEXT, name TEXT, agent_nickname TEXT, created_at_ms INTEGER)')
            db.execute('CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT, status TEXT)')
            db.executemany('INSERT INTO threads VALUES (?,?,?,?)', [('main','Main',None,0),('a','a','Alpha',3),('z','z','Zulu',1),('c','c','Closed',2)])
            db.executemany('INSERT INTO thread_spawn_edges VALUES (?,?,?)', [('main','a','open'),('main','c','closed'),('main','z','open')])
        with sqlite3.connect(directory / 'thread_history_1.sqlite') as db:
            db.execute('CREATE TABLE thread_turns (thread_id TEXT, status TEXT, rollout_ordinal INTEGER)')
            db.executemany('INSERT INTO thread_turns VALUES (?,?,?)', [('a','inProgress',1),('z','completed',1)])
        parts = status.agent_overview('main', {}, self.root, structured=True)
        self.assertEqual([style.native for _,style in parts if isinstance(style,status.AgentStyle)], ['main','a','z','c'])
        self.assertIn(('Closed — закрыт','closed'), parts)
        self.assertIn('Alpha ●', status.agent_panel_rows(parts, 50)[1])
        with sqlite3.connect(directory / 'thread_history_1.sqlite') as db:
            db.executemany('INSERT INTO thread_turns VALUES (?,?,?)', [('a','completed',2),('z','inProgress',2)])
        refreshed = status.agent_overview('main', {}, self.root, structured=True)
        self.assertEqual([style.native for _,style in refreshed if isinstance(style,status.AgentStyle)], ['main','z','a','c'])
        self.assertIn(('Zulu — работает','working'), refreshed)
        self.assertIn(('Alpha — готов',''), refreshed)

    def test_rollout_lifecycle_updates_agents_before_history_index(self):
        directory = self.root / '.codex'; directory.mkdir()
        rollout = directory / 'agent.jsonl'
        with sqlite3.connect(directory / 'state_5.sqlite') as db:
            db.execute('CREATE TABLE threads (id TEXT, name TEXT, agent_nickname TEXT, rollout_path TEXT)')
            db.execute('CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT, status TEXT)')
            db.executemany('INSERT INTO threads VALUES (?,?,?,?)', [('main','Main',None,None),('a','task','Fresh',str(rollout))])
            db.execute('INSERT INTO thread_spawn_edges VALUES (?,?,?)', ('main','a','open'))
        with sqlite3.connect(directory / 'thread_history_1.sqlite') as db:
            db.execute('CREATE TABLE thread_turns (thread_id TEXT, status TEXT, rollout_ordinal INTEGER, started_at INTEGER, completed_at INTEGER)')
            # A resumed rollout can have a lower ordinal than an older turn.
            db.executemany('INSERT INTO thread_turns VALUES (?,?,?,?,?)', [('a','inProgress',900,1,None),('a','completed',1,2,3)])
        self.assertIn('Fresh — готов', status.agent_overview('main', {}, self.root))
        for event, expected in [('task_started','работает'), ('task_complete','готов'), ('task_started','работает'), ('turn_aborted','прерван')]:
            with rollout.open('a') as output:
                output.write(json.dumps({'timestamp':'2026-11-11T16:00:00Z','type':'event_msg','payload':{'type':event}})+'\n')
            self.assertIn('Fresh — '+expected, status.agent_overview('main', {}, self.root))
        # A later indexed interruption must not be replaced by an old start.
        self.assertEqual(status.latest_agent_status(rollout, 'failed', 9999999999), 'failed')

    def test_active_agent_after_128_old_agents_is_still_visible(self):
        directory = self.root / '.codex'; directory.mkdir()
        with sqlite3.connect(directory / 'state_5.sqlite') as db:
            db.execute('CREATE TABLE threads (id TEXT, name TEXT, agent_nickname TEXT)')
            db.execute('CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT, status TEXT)')
            db.execute('INSERT INTO threads VALUES (?,?,?)', ('main','Main',None))
            for index in range(130):
                ident = str(index)
                db.execute('INSERT INTO threads VALUES (?,?,?)', (ident,'task','Agent'+ident))
                db.execute('INSERT INTO thread_spawn_edges VALUES (?,?,?)', ('main',ident,'closed' if index < 129 else 'open'))
        with sqlite3.connect(directory / 'thread_history_1.sqlite') as db:
            db.execute('CREATE TABLE thread_turns (thread_id TEXT, status TEXT, rollout_ordinal INTEGER)')
            db.execute('INSERT INTO thread_turns VALUES (?,?,?)', ('129','inProgress',1))
        parts = status.agent_overview('main', {}, self.root, structured=True)
        self.assertEqual(parts[1], ('Agent129 — работает','working'))
        line = status.agent_panel_rows(parts, 70, tmux=True)[1]
        self.assertIn('Agent129 ●', line)
        self.assertIn('fg=colour117,bold', line)

    def test_metrics_use_exact_thread_and_actual_quota_window(self):
        directory = self.root / '.codex'; directory.mkdir()
        rollout = directory / 'thread.jsonl'
        event = {'type':'event_msg','payload':{'type':'token_count','info':{'last_token_usage':{'total_tokens':95000},'model_context_window':475000},
            'rate_limits':{'primary':{'window_minutes':10080,'used_percent':46,'resets_at':9999999999}}}}
        rollout.write_text(json.dumps(event)+'\n')
        with sqlite3.connect(directory / 'state_5.sqlite') as db:
            db.execute('CREATE TABLE threads (id TEXT, rollout_path TEXT, model TEXT, reasoning_effort TEXT)')
            db.execute('INSERT INTO threads VALUES (?,?,?,?)', ('ours',str(rollout),'gpt-6.1-sol','medium'))
        value = status.session_metrics('ours', {}, self.root)
        self.assertIn('95K / 475K', value)
        self.assertIn('gpt-6.1-sol medium', value)
        self.assertNotIn('снимок', value)
        self.assertNotIn('посл. данные', value)
        self.assertIn('неделя: 54% осталось', value)
        self.assertEqual(status.session_metrics('other', {}, self.root), '')
        event['payload']['rate_limits']['primary']['resets_at'] = 1
        rollout.write_text(json.dumps(event)+'\n')
        self.assertIn('неделя: ожидает данных', status.session_metrics('ours', {}, self.root))
        rows = status.agent_panel_rows([('Основной: Main','main')], 120, metrics='Основной · '+value)
        self.assertLessEqual(len(rows),4)
        self.assertIn('475K', '\n'.join(rows))
        self.assertIn('неделя:', '\n'.join(rows))


    def test_rename_is_read_on_each_refresh_for_exact_session(self):
        directory = self.root / '.codex'; directory.mkdir()
        with sqlite3.connect(directory / 'state_5.sqlite') as connection:
            connection.execute('CREATE TABLE threads (id TEXT, name TEXT)')
            connection.executemany('INSERT INTO threads VALUES (?, ?)', [('ours', 'Моя сессия'), ('other', 'Чужая сессия')])
            connection.commit()
            first = status.collect(self.project, 'ours', self.environment, self.root)
            self.assertEqual(first['session_name'], 'Моя сессия')
            connection.execute('UPDATE threads SET name = ? WHERE id = ?', ('Новое имя', 'ours'))
            connection.commit()
            second = status.collect(self.project, 'ours', self.environment, self.root)
        self.assertEqual(second['session_name'], 'Новое имя')
        self.assertIn('Новое имя', status.render(second, {}))
        self.assertNotIn('session_name', status.collect(self.project, '', self.environment, self.root))

    def test_index_fallback_uses_latest_exact_session_name_and_custom_codex_home(self):
        directory = self.root / 'custom'; directory.mkdir()
        index = directory / 'session_index.jsonl'
        index.write_text('\n'.join(json.dumps(entry) for entry in [[], {'id':'ours', 'thread_name':'Старое'},
                         {'id':'other', 'thread_name':'Чужое'}, {'id':'ours', 'thread_name':'Переименовано'}]) + '\n')
        environment = dict(self.environment, CODEX_HOME=str(directory))
        self.assertEqual(status.collect(self.project, 'ours', environment, self.root)['session_name'], 'Переименовано')

    def test_session_name_is_safe_for_tmux_and_can_be_hidden(self):
        value = {'project':'project', 'session_name':'Моё имя\n\033#[fg=red]'}
        rendered = status.render(value, {}, tmux=True)
        self.assertIn('Моё имя', rendered)
        self.assertIn('##[', rendered)
        self.assertNotIn('\033', rendered)
        self.assertNotIn('\n', rendered)
        self.assertEqual(status.render(value, {'fields':['project']}), 'project')

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
