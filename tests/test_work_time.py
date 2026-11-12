import importlib.util
import pathlib
import sqlite3
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('work_status', pathlib.Path(__file__).parents[1] / 'scripts/status.py')
status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status)
ROOT = '01a17355-a85e-7891-8c30-36881eade687'
CHILD = '01a17355-a85e-7891-8c30-36881eade688'


class WorkTimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = pathlib.Path(temporary.name)
        self.directory = self.home / '.codex'
        self.directory.mkdir()
        rollout = self.directory / 'empty.jsonl'
        rollout.touch()
        with sqlite3.connect(self.directory / 'state_5.sqlite') as db:
            db.execute('CREATE TABLE threads (id TEXT, rollout_path TEXT)')
            db.executemany('INSERT INTO threads VALUES (?,?)', [(ROOT,str(rollout)),(CHILD,str(rollout))])
            db.execute('CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT, status TEXT)')
            db.execute('INSERT INTO thread_spawn_edges VALUES (?,?,?)', (ROOT,CHILD,'open'))
        with sqlite3.connect(self.directory / 'thread_history_1.sqlite') as db:
            db.execute('CREATE TABLE thread_turns (thread_id TEXT, status TEXT, started_at INTEGER, completed_at INTEGER, duration_ms INTEGER)')
            db.executemany('INSERT INTO thread_turns VALUES (?,?,?,?,?)', [
                (ROOT,'completed',100,200,100000), (CHILD,'completed',150,250,100000),
                (ROOT,'completed',300,360,60000), (ROOT,'interrupted',400,410,10000),
                (ROOT,'inProgress',450,None,None)])

    def read(self, **kwargs):
        return status.session_work_time(ROOT, {}, self.home, now=660, **kwargs)

    def test_historical_intervals_exclude_idle_and_union_parallel_agents(self):
        value = self.read()
        self.assertEqual(value['total_seconds'], 220)
        self.assertIsNone(value['current_seconds'])
        self.assertFalse(value['live'])
        self.assertNotIn('сейчас:', status.work_time_label(value))

    def test_old_unfinished_turn_does_not_advance_after_resume(self):
        self.assertEqual(self.read(live_since=500)['total_seconds'], 220)
        self.assertIn('сейчас: пауза', status.work_time_label(self.read(live_since=500)))

    def test_live_turns_advance_once_and_reset_survives_next_read(self):
        with sqlite3.connect(self.directory / 'thread_history_1.sqlite') as db:
            db.executemany('INSERT INTO thread_turns VALUES (?,?,?,?,?)', [
                (ROOT,'inProgress',600,None,None), (CHILD,'inProgress',610,None,None)])
        value = self.read(live_since=500)
        self.assertEqual(value['total_seconds'], 280)
        self.assertEqual(value['current_seconds'], 60)
        status.session_work_time(ROOT, {}, self.home, live_since=500, reset_task=True, now=630)
        value = self.read(live_since=500)
        self.assertEqual(value['total_seconds'], 280)
        self.assertEqual(value['task_seconds'], 30)
        marker = self.home / '.local/share/codex-tools/timers' / (ROOT + '.json')
        self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
        child = status.session_work_time(CHILD, {}, self.home, live_since=500, now=660)
        self.assertEqual(child, value)

    def test_closed_child_is_counted_historically_but_never_ticking(self):
        with sqlite3.connect(self.directory / 'state_5.sqlite') as db:
            db.execute("UPDATE thread_spawn_edges SET status='closed'")
        with sqlite3.connect(self.directory / 'thread_history_1.sqlite') as db:
            db.execute('INSERT INTO thread_turns VALUES (?,?,?,?,?)', (CHILD,'inProgress',600,None,None))
        self.assertEqual(self.read(live_since=500)['total_seconds'], 220)

    def test_missing_history_is_unknown_rather_than_zero(self):
        (self.directory / 'thread_history_1.sqlite').unlink()
        self.assertIsNone(self.read())
        self.assertEqual(status.work_time_label(None), 'Работа: история недоступна')

    def test_clock_has_bold_contrasting_row_without_hiding_metrics(self):
        rows = status.agent_panel_rows([('Основной: Main','main')], 100, tmux=True,
                                       metrics='model medium · контекст: 10K/475K · неделя: 43%',
                                       timing='Работа: 2ч 18м · сейчас: 7м')
        self.assertEqual(len(rows), 4)
        self.assertIn('fg=colour229,bold', rows[2])
        self.assertIn('сейчас: 7м', rows[2])
        self.assertIn('контекст:', rows[3])
        self.assertIn('неделя: 43%', rows[3])


if __name__ == '__main__':
    unittest.main()
