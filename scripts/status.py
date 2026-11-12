#!/usr/bin/python3
import argparse
import datetime
import json
import os
import pathlib
import re
import sqlite3
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.parse

FIELDS = ['project', 'session_name', 'environment', 'address', 'listener', 'scope', 'browser']

def clean(value):
    return ''.join(character for character in str(value) if not unicodedata.category(character).startswith('C'))[:500]

def project_root(cwd):
    path = pathlib.Path(cwd).resolve()
    for parent in [path, *path.parents]:
        if (parent / 'devops/project.conf').is_file() and (parent / 'devops/support/listeners.sh').is_file():
            return parent
    return path

def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, ValueError, OverflowError):
        return False

def read(path):
    try:
        return path.read_text().strip()
    except OSError:
        return ''

def session_name(session, environment, home):
    if not session:
        return ''
    directory = pathlib.Path(environment.get('CODEX_HOME') or home / '.codex')
    database = directory / 'state_5.sqlite'
    if database.is_file():
        connection = None
        try:
            connection = sqlite3.connect('file:' + str(database) + '?mode=ro', uri=True, timeout=0.2)
            row = connection.execute('SELECT name FROM threads WHERE id = ?', (session,)).fetchone()
            if row and isinstance(row[0], str) and row[0].strip():
                return row[0].strip()
        except sqlite3.Error:
            pass
        finally:
            if connection is not None:
                connection.close()
    name = ''
    try:
        with (directory / 'session_index.jsonl').open('rb') as stream:
            stream.seek(0, 2)
            offset = max(0, stream.tell() - 5_000_000)
            stream.seek(offset)
            if offset:
                stream.readline()
            for line in stream:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and entry.get('id') == session and isinstance(entry.get('thread_name'), str):
                    name = entry['thread_name'].strip()
    except OSError:
        pass
    return name

class AgentStyle(str):
    def __new__(cls, value, native, running=False):
        item = super().__new__(cls, value)
        item.native = native
        item.running = running
        return item

def latest_agent_status(rollout, fallback, recorded_at=0):
    """Read lifecycle metadata that may reach the rollout before the history index."""
    if not rollout:
        return fallback
    states = {'task_started': 'inProgress', 'task_complete': 'completed',
              'turn_aborted': 'interrupted', 'task_failed': 'failed'}
    try:
        with pathlib.Path(rollout).open('rb') as source:
            source.seek(0, 2)
            source.seek(max(0, source.tell() - 262144))
            lines = source.read().splitlines()
        for line in reversed(lines):
            try:
                event = json.loads(line)
                payload = event.get('payload', {})
                if event.get('type') != 'event_msg' or payload.get('type') not in states:
                    continue
                at = datetime.datetime.fromisoformat(event['timestamp'].replace('Z', '+00:00')).timestamp()
                # History timestamps have second precision. A recorded terminal
                # state wins ties against an earlier start within that second.
                boundary = recorded_at + (1 if fallback in ('completed', 'interrupted', 'failed') else 0)
                return states[payload['type']] if at >= boundary else fallback
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
    except (OSError, ValueError):
        pass
    return fallback

def agent_overview(session, environment, home, structured=False, selected=None, show_selection=False):
    """Read only explicit spawn edges and persisted turn states, never messages."""
    if not session:
        return ''
    directory = pathlib.Path(environment.get('CODEX_HOME') or home / '.codex')
    connection = history = None
    try:
        connection = sqlite3.connect((directory / 'state_5.sqlite').as_uri() + '?mode=ro', uri=True, timeout=0.2)
        root = session
        seen = set()
        for _ in range(32):
            if root in seen:
                return 'Агенты: данные недоступны'
            seen.add(root)
            parent = connection.execute('SELECT parent_thread_id FROM thread_spawn_edges WHERE child_thread_id = ?', (root,)).fetchone()
            if not parent:
                break
            root = parent[0]
        else:
            return 'Агенты: данные недоступны'
        name = connection.execute('SELECT name FROM threads WHERE id = ?', (root,)).fetchone()
        if not name:
            return ''
        label = name[0] or 'Без названия'
        navigation = [('⌥←/→: переключить · /subagents: список', False)] if show_selection else []
        columns = {row[1] for row in connection.execute('PRAGMA table_info(threads)')}
        rollout_column = 't.rollout_path' if 'rollout_path' in columns else 'NULL'
        order = 't.created_at_ms, t.id' if 'created_at_ms' in columns else 't.created_at, t.id' if 'created_at' in columns else 't.rowid'
        children = connection.execute('''WITH RECURSIVE family(id) AS (
            SELECT ? UNION SELECT e.child_thread_id FROM thread_spawn_edges e JOIN family f ON e.parent_thread_id = f.id
        ) SELECT t.id, t.agent_nickname, t.name, e.status, ''' + rollout_column + ''' FROM family f
          JOIN threads t ON t.id = f.id JOIN thread_spawn_edges e ON e.child_thread_id = t.id
          WHERE t.id != ? ORDER BY ''' + order, (root, root)).fetchall()
        selected = resolve_visible_agent(selected, [root] + [child[0] for child in children])
        main_part = (('▶ ' if selected == root else '') + 'Основной: ' + label,
                     AgentStyle('selected' if selected == root else 'main', root))
        if not children:
            parts = [main_part] + navigation + [('Субагенты: нет', False)]
            if show_selection and selected != root:
                parts.append(('Выбранный: неизвестен; /statusline → thread-id', False))
            return parts if structured else ' · '.join(text for text, _ in parts)
        try:
            history = sqlite3.connect((directory / 'thread_history_1.sqlite').as_uri() + '?mode=ro', uri=True, timeout=0.2)
            turn_columns = {row[1] for row in history.execute('PRAGMA table_info(thread_turns)')}
        except sqlite3.Error:
            pass
        active = []
        closed = 0
        states = {'inProgress': 'работает', 'completed': 'готов', 'failed': 'ошибка', 'interrupted': 'прерван'}
        for native, nickname, title, edge, rollout in children:
            if edge == 'closed':
                closed += 1
                active.append(('закрыт', nickname or title or 'Без названия', native))
                continue
            status = None
            recorded_at = 0
            if history is not None:
                try:
                    has_times = {'started_at', 'completed_at'} <= turn_columns
                    times = ', started_at, completed_at' if has_times else ''
                    ordering = 'started_at DESC, rowid DESC' if has_times else 'rollout_ordinal DESC'
                    row = history.execute('SELECT status' + times + ' FROM thread_turns WHERE thread_id = ? ORDER BY ' + ordering + ' LIMIT 1', (native,)).fetchone()
                    status = row[0] if row else None
                    if row and has_times:
                        recorded_at = row[2] or row[1] or 0
                except sqlite3.Error:
                    pass
            status = latest_agent_status(rollout, status, recorded_at)
            active.append((states.get(status, 'статус неизвестен'), nickname or title or 'Без названия', native))
        # Keep creation order within each group, but never truncate working
        # agents behind old completed or closed threads.
        active.sort(key=lambda item: 0 if item[0] == 'работает' else 2 if item[0] == 'закрыт' else 1)
        parts = [main_part] + navigation
        parts.extend((('▶ ' if selected == native else '') + name + ' — ' + state,
                      AgentStyle('selected' if selected == native else 'working' if state == 'работает' else 'closed' if state == 'закрыт' else '', native,
                                 running=state == 'работает')) for state, name, native in active)
        if show_selection and selected not in {root, *(native for _, _, native in active)}:
            parts.insert(1, ('Выбранный: неизвестен; /statusline → thread-id', False))
        if closed:
            parts.append(('закрыты: ' + str(closed), False))
        return parts if structured else ' · '.join(text for text, _ in parts)
    except (OSError, ValueError, sqlite3.Error):
        return 'Агенты: данные недоступны'
    finally:
        if history is not None:
            history.close()
        if connection is not None:
            connection.close()

def cell_width(text):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in text)

def merged_seconds(intervals, since=0):
    total = 0
    end = 0
    for start, stop in sorted(intervals):
        start = max(start, since, end)
        if stop > start:
            total += stop - start
            end = stop
    return total

def work_duration(seconds):
    minutes = max(0, int(seconds)) // 60
    hours, minutes = divmod(minutes, 60)
    return (str(hours) + 'ч ' if hours else '') + str(minutes) + 'м'

def session_work_time(session, environment, home, live_since=None, reset_task=False, now=None):
    """Union of recorded turn intervals, including descendants, without idle gaps."""
    now = time.time() if now is None else now
    directory = pathlib.Path(environment.get('CODEX_HOME') or home / '.codex')
    if not session:
        return None
    try:
        with sqlite3.connect((directory / 'state_5.sqlite').as_uri() + '?mode=ro', uri=True, timeout=0.2) as db:
            root = session
            seen = set()
            while root not in seen:
                seen.add(root)
                parent = db.execute('SELECT parent_thread_id FROM thread_spawn_edges WHERE child_thread_id=?', (root,)).fetchone()
                if not parent: break
                root = parent[0]
            else:
                return None
            if not db.execute('SELECT 1 FROM threads WHERE id=?', (root,)).fetchone(): return None
            if not re.fullmatch(r'[a-f0-9-]{36}', root): return None
            members = db.execute('''WITH RECURSIVE family(id) AS (
                SELECT ? UNION SELECT e.child_thread_id FROM thread_spawn_edges e JOIN family f ON e.parent_thread_id=f.id
            ) SELECT t.id, t.rollout_path, COALESCE(e.status, 'open') FROM family f
              JOIN threads t ON t.id=f.id LEFT JOIN thread_spawn_edges e ON e.child_thread_id=t.id''', (root,)).fetchall()
        intervals, ongoing = [], []
        with sqlite3.connect((directory / 'thread_history_1.sqlite').as_uri() + '?mode=ro', uri=True, timeout=0.2) as history:
            for native, rollout, edge in members:
                turns = history.execute('SELECT status, started_at, completed_at, duration_ms FROM thread_turns WHERE thread_id=? ORDER BY started_at, rowid', (native,)).fetchall()
                for index, (state, start, complete, duration) in enumerate(turns):
                    if not isinstance(start, (int, float)) or start <= 0: continue
                    stop = start + max(0, duration) / 1000 if isinstance(duration, (int, float)) else complete
                    # Only a current turn from this live run can advance a clock.
                    # Old unfinished turns never grow through days of inactivity.
                    if (state == 'inProgress' and index == len(turns) - 1 and edge != 'closed'
                            and live_since is not None and start >= int(live_since)
                            and latest_agent_status(rollout, state, start) == 'inProgress'):
                        stop = now
                        ongoing.append(start)
                    if isinstance(stop, (int, float)) and stop >= start:
                        intervals.append((start, min(stop, now)))
        marker = home / '.local/share/codex-tools/timers' / (root + '.json')
        since = None
        if reset_task:
            marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = marker.with_name(marker.name + '.' + str(os.getpid()))
            try:
                temporary.write_text(json.dumps({'task_started_at': now}))
                temporary.chmod(0o600)
                os.replace(temporary, marker)
            finally:
                temporary.unlink(missing_ok=True)
        try:
            since = json.loads(marker.read_text()).get('task_started_at')
        except (OSError, ValueError, AttributeError):
            pass
        return {'session_id': root, 'total_seconds': merged_seconds(intervals), 'live': live_since is not None,
                'current_seconds': max(0, now - min(ongoing)) if ongoing else None,
                'task_seconds': merged_seconds(intervals, since) if isinstance(since, (int, float)) else None}
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return None

def work_time_label(value):
    if value is None: return 'Работа: история недоступна'
    parts = ['Работа: ' + work_duration(value['total_seconds'])]
    if value['task_seconds'] is not None: parts.append('задача: ' + work_duration(value['task_seconds']))
    if value['live']:
        parts.append('сейчас: ' + work_duration(value['current_seconds']) if value['current_seconds'] is not None else 'сейчас: пауза')
    return ' · '.join(parts)

def session_metrics(session, environment, home, run=None):
    """Use only persisted token metadata; never infer current quota after a reset."""
    cache = None
    if run and re.fullmatch(r'[a-f0-9]{32}', run):
        cache = home / '.local/share/codex-tools/runs' / run / 'panel-metrics.json'
        try:
            saved = json.loads(cache.read_text())
            if saved.get('format') == 3 and saved.get('session') == session and 0 <= time.time() - saved.get('at', 0) < 30:
                return saved.get('text', '')
        except (OSError, ValueError, TypeError):
            pass
    text = ''
    directory = pathlib.Path(environment.get('CODEX_HOME') or home / '.codex')
    connection = None
    try:
        connection = sqlite3.connect((directory / 'state_5.sqlite').as_uri() + '?mode=ro', uri=True, timeout=0.2)
        columns = {item[1] for item in connection.execute('PRAGMA table_info(threads)')}
        model_column = 'model' if 'model' in columns else 'NULL'
        effort_column = 'reasoning_effort' if 'reasoning_effort' in columns else 'NULL'
        row = connection.execute('SELECT rollout_path, ' + model_column + ', ' + effort_column + ' FROM threads WHERE id = ?', (session,)).fetchone()
        if row:
            with pathlib.Path(row[0]).open('rb') as stream:
                stream.seek(0, 2)
                start = max(0, stream.tell() - 1_500_000)
                stream.seek(start)
                if start: stream.readline()
                lines = stream.readlines()
            info = rates = None
            for raw in reversed(lines):
                try: event = json.loads(raw)
                except ValueError: continue
                if not isinstance(event, dict): continue
                payload = event.get('payload')
                if event.get('type') != 'event_msg' or not isinstance(payload, dict) or payload.get('type') != 'token_count':
                    continue
                if info is None and isinstance(payload.get('info'), dict): info = payload['info']
                if rates is None and isinstance(payload.get('rate_limits'), dict): rates = payload['rate_limits']
                if info is not None and rates is not None: break
            info, rates = info or {}, rates or {}
            usage = (info.get('last_token_usage') or {}).get('total_tokens')
            window = info.get('model_context_window')
            model = clean(row[1]) if isinstance(row[1], str) else ''
            effort = clean(row[2]) if isinstance(row[2], str) else ''
            parts = [' '.join(value for value in (model, effort) if value)] if model or effort else []
            if isinstance(usage, (int, float)) and isinstance(window, (int, float)) and window > 0:
                parts.append('контекст: ' + str(round(usage / 1000)) + 'K / ' + str(round(window / 1000)) + 'K')
            for key in ('primary', 'secondary'):
                rate = rates.get(key) or {}
                used, minutes = rate.get('used_percent'), rate.get('window_minutes')
                if not isinstance(used, (int, float)) or not isinstance(minutes, int): continue
                label = 'неделя' if minutes == 10080 else '5ч' if minutes == 300 else str(minutes) + 'мин'
                reset = rate.get('resets_at')
                value = 'ожидает данных' if isinstance(reset, (int, float)) and reset <= time.time() else str(round(max(0, min(100, 100 - used)))) + '% осталось'
                parts.append(label + ': ' + value)
            text = ' · '.join(parts) if parts else ''
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
        pass
    finally:
        if connection is not None: connection.close()
    if cache is not None:
        temporary = cache.with_name(cache.name + '.' + str(os.getpid()))
        try:
            temporary.write_text(json.dumps({'format': 3, 'session': session, 'at': time.time(), 'text': text}, ensure_ascii=False))
            temporary.chmod(0o600)
            os.replace(temporary, cache)
        except OSError:
            temporary.unlink(missing_ok=True)
    return text

def agent_rows(overview, width, tmux=False, max_rows=4):
    """Wrap whole entries where possible; keep trusted styles apart from names."""
    width = max(16, min(width, 1000))
    capacity = width - cell_width(' АГЕНТЫ │ ')
    entries = overview if isinstance(overview, list) else [(overview, False)]
    rows = [[]]
    used = 0
    for text, working in entries:
        text = clean(text)
        if not text:
            continue
        if used and used + 3 + cell_width(text) > capacity:
            rows.append([]); used = 0
        if used:
            rows[-1].append((' · ', False)); used += 3
        for char in text:
            size = cell_width(char)
            if used + size > capacity:
                rows.append([]); used = 0
            rows[-1].append((char, working)); used += size
    if len(rows) > max_rows:
        rows = rows[:max_rows]
        while rows[-1] and sum(cell_width(text) for text, _ in rows[-1]) + cell_width(' · …') > capacity:
            rows[-1].pop()
        rows[-1].append((' · …', False))
    rendered = []
    for index, row in enumerate(rows):
        prefix = ' АГЕНТЫ │ ' if index == 0 else '        │ '
        if not tmux:
            rendered.append(prefix + ''.join(text for text, _ in row))
            continue
        result = '#[bg=colour236,fg=colour250,bold]' + prefix
        previous = None
        for text, working in row:
            if working != previous:
                result += ('#[bg=colour238,fg=colour229,bold]' if working == 'selected' else
                           '#[bg=colour236,fg=colour255,bold]' if working == 'main' else
                           '#[bg=colour236,fg=colour244,nobold]' if working == 'closed' else
                           '#[bg=colour236,fg=colour117,bold]' if working else '#[bg=colour236,fg=colour255,nobold]')
                previous = working
            result += text.replace('#', '##')
        result += '#[norange,bg=colour236,fg=colour255,nobold]'
        rendered.append(result)
    return rendered

def agent_panel_rows(overview, width, tmux=False, metrics="", timing=""):
    """One compact agent row, padding around the group, and a shared info row."""
    width = max(16, min(width, 1000))
    def clip(text, limit):
        result = ''
        for char in clean(text):
            if cell_width(result + char) > limit: break
            result += char
        return result
    symbols = {'работает': '●', 'готов': '✓', 'закрыт': '×',
               'ошибка': '!', 'прерван': '■', 'статус неизвестен': '?'}
    entries = overview if isinstance(overview, list) else [(overview, False)]
    items = []
    selected = ''
    for text, style in entries:
        text = clean(text)
        if isinstance(overview, list) and not isinstance(style, AgentStyle) and style not in ('main', 'selected', 'working', 'closed', ''):
            continue
        if style == 'selected': selected = text.removeprefix('▶ ').split(' — ')[0]
        name, separator, state = text.rpartition(' — ')
        if separator and state in symbols: text = name + ' ' + symbols[state]
        if text: items.append((text, style))
    capacity = width - 4
    shown = []
    used = 0
    for index, (text, style) in enumerate(items):
        gap = 3 if shown else 0
        reserve = 4 if index < len(items) - 1 else 0
        if shown and used + gap + cell_width(text) + reserve > capacity: break
        text = clip(text, capacity - used - gap - reserve)
        if shown: shown.append((' · ', False)); used += gap
        shown.append((text, style)); used += cell_width(text)
    # Entries with an unknown state may have an empty style; count actual entries separately.
    displayed = sum(text != ' · ' for text, _ in shown)
    if displayed < len(items): shown.append((' · …', False)); used += 4
    def styled(text, style):
        if not tmux: return text
        colors = ('bg=colour238,fg=colour229,bold' if style == 'selected' else
                  'bg=colour237,fg=colour255,bold' if style == 'main' else
                  'bg=colour237,fg=colour117,bold' if style == 'working' else
                  'bg=colour237,fg=colour245,nobold' if style == 'closed' else
                  'bg=colour237,fg=colour250,nobold' if style is not False else
                  'bg=colour236,fg=colour250,nobold')
        return '#[' + colors + ']' + text.replace('#', '##')
    blank = styled(' ' * width, False)
    line = styled('  ', False) + ''.join(styled(text, style) for text, style in shown)
    line += styled(' ' * max(2, width - 2 - used), False)
    data = clean(metrics)
    for old, new in [('Выбранный · ', ''), ('Основной · ', ''), ('посл. данные · ', ''),
                     ('снимок · ', ''), (' / ', '/')]:
        data = data.replace(old, new)
    who = ('Сейчас: ' + ('основной' if selected.startswith('Основной:') else clip(selected, 20))) if selected else 'Выбор: Codex'
    if not selected and data: who = ''
    controls = ' /subagents · ⌥←/→ ' if width >= 100 else ' /subagents '
    available = max(0, width - cell_width(controls))
    info = clip(' ' + ' · '.join(part for part in (who, data) if part), available)
    info += ' ' * max(0, available - cell_width(info)) + clip(controls, width - cell_width(info))
    if tmux: info = '#[bg=colour234,fg=colour248,nobold]' + info.replace('#', '##')
    clock = clip('  ' + timing, width) if timing else ''
    if tmux and clock: clock = '#[bg=colour236,fg=colour229,bold]' + clock.replace('#', '##') + '#[nobold]'
    return [blank, line, clock or blank, info]

def footer_thread_id(text):
    # Native footer may put thread-id last and truncate it to the available width.
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for index, line in enumerate(reversed(lines[-3:])):
        match = re.fullmatch(r'([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})(?:\s+·.*)?', line)
        if match and (index == 0 or ' · ' in line):
            return match[1]
        if not any(marker in line for marker in ('Context ', 'weekly ', 'window', ' · ')) or line.startswith(('›', '•')):
            continue
        match = re.search(r'(?:^| · )([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})(?= · |$)', line)
        if match:
            return match[1]
        match = re.search(r'(?:^| · )([0-9a-f]{8}-[0-9a-f-]{9,26})…$', line)
        if match and len(match[1].replace('-', '')) >= 16:
            return match[1] + '…'
    return None

def resolve_visible_agent(selected, family):
    if not isinstance(selected, str):
        return None
    if selected.endswith('…'):
        matches = [native for native in set(family) if native.startswith(selected[:-1])]
        return matches[0] if len(matches) == 1 else None
    return selected if selected in family else None

def visible_agent(run, home):
    if not re.fullmatch(r'[a-f0-9]{32}', run):
        return None
    executable = home / '.local/share/codex-tools/tmux/bin/tmux'
    tmux = str(executable) if executable.is_file() else shutil.which('tmux')
    if not tmux:
        return None
    prefix = [tmux, '-L', 'codex-tools-' + run, '-f', '/dev/null']
    try:
        size = subprocess.run([*prefix, 'display-message', '-p', '-t', 'codex', '#{pane_height} #{cursor_y}'], capture_output=True, text=True, timeout=1)
        fields = size.stdout.strip().split()
        if size.returncode or len(fields) != 2 or not all(field.isdigit() for field in fields):
            return None
        height, cursor = map(int, fields)
        captured = subprocess.run([*prefix, 'capture-pane', '-p', '-t', 'codex', '-S', str(max(0, min(cursor, height - 8))), '-E', str(height - 1)],
                                  capture_output=True, text=True, timeout=1)
        return footer_thread_id(captured.stdout) if captured.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None

def update_agent_height(run, row_count, home):
    if not re.fullmatch(r'[a-f0-9]{32}', run):
        return
    installed = home / '.local/share/codex-tools/tmux/bin/tmux'
    tmux = str(installed) if installed.is_file() else shutil.which('tmux')
    if not tmux:
        return
    prefix = [tmux, '-L', 'codex-tools-' + run, '-f', '/dev/null']
    desired = str(1 + max(1, min(4, row_count)))
    try:
        current = subprocess.run([*prefix, 'show-options', '-gv', 'status'], capture_output=True, text=True, timeout=1)
        if current.returncode == 0 and current.stdout.strip() != desired:
            subprocess.run([*prefix, 'set-option', '-g', 'status', desired],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        pass

def collect(cwd, session, environment, home):
    root = project_root(cwd)
    result = {'project': root.name}
    name = session_name(session, environment, home)
    if name:
        result['session_name'] = name
    configuration = root / 'devops/project.conf'
    if configuration.is_file() and (root / 'devops/support/listeners.sh').is_file():
        values = dict(re.findall(r'^([A-Z_]+)="([^"\n]*)"\s*$', read(configuration), re.MULTILINE))
        result['project'] = values.get('PROJECT_NAME', root.name)
        url = environment.get('EMPIRIKA_BASE_URL') or 'https://' + values.get('PROD_DOMAIN', '')
        address = urllib.parse.urlsplit(url)
        host = address.hostname or ''
        stand = 'prod' if host == values.get('PROD_DOMAIN') else 'dev' if host == values.get('DEV_DOMAIN') else 'local'
        result['environment'] = {'prod': 'бой', 'dev': 'тестовый', 'local': 'локальный'}[stand]
        result['address'] = host + ((':' + str(address.port)) if address.port else '')
        result['listener'] = 'сессия не определена' if not session else 'слушателя шины нет'
        directory = pathlib.Path(environment.get('EMPIRIKA_LISTENERS_DIR') or home / '.cache/empirika/listeners')
        if session:
            for path in sorted(directory.glob('*.stand')):
                if not path.stem.isdigit() or not alive(int(path.stem)) or read(path) != stand:
                    continue
                theirs = read(path.with_suffix('.session'))
                if theirs != re.sub(r'[^A-Za-z0-9_-]', '_', session):
                    continue
                result['listener'] = 'слушатель шины жив'
                result['scope'] = ' · '.join(part for part in read(path.with_suffix('.filter')).splitlines() if part) or 'все проекты'
                break
    guard = home / '.local/share/codex-browser-guard/state/status.json'
    if guard.exists():
        try:
            health = json.loads(guard.read_text())
            result['browser'] = ('браузер: CLI проверен' if health.get('healthy') else
                                 'проверка браузера: требует обновления' if 'Unsupported cua_repl launcher' in str(health.get('error', '')) else
                                 'браузер: нужна проверка подключения')
        except (ValueError, OSError):
            result['browser'] = 'браузер: статус неизвестен'
    return result

def render(value, settings, tmux=False):
    fields = settings.get('fields', FIELDS)
    if not isinstance(fields, list) or any(field not in FIELDS for field in fields):
        raise ValueError('fields must be a list of known field names')
    text = clean(settings.get('separator', ' · ')).join(clean(value[field]) for field in fields if value.get(field))
    return text.replace('#', '##') if tmux else text

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cwd', default=os.getcwd())
    parser.add_argument('--session', default=os.environ.get('CODEX_THREAD_ID', ''))
    parser.add_argument('--run')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--tmux', action='store_true')
    parser.add_argument('--agents', action='store_true')
    parser.add_argument('--agent-row', type=int)
    parser.add_argument('--width', type=int, default=120)
    parser.add_argument('--work-time', action='store_true')
    parser.add_argument('--reset-task', action='store_true')
    args = parser.parse_args()
    home = pathlib.Path.home()
    environment = dict(os.environ)
    live_since = None
    if args.run:
        args.session = ''
        if not re.fullmatch(r'[a-f0-9]{32}', args.run):
            raise ValueError('Invalid run identifier')
        state = home / '.local/share/codex-tools/runs' / args.run
        launch = json.loads((state / 'launch.json').read_text())
        live_since = (state / 'launch.json').stat().st_mtime
        if args.work_time or (args.agents and args.agent_row == 2):
            installed_tmux = home / '.local/share/codex-tools/tmux/bin/tmux'
            tmux = str(installed_tmux) if installed_tmux.is_file() else shutil.which('tmux')
            try:
                check = subprocess.run([tmux, '-L', 'codex-tools-' + args.run, 'list-panes', '-t', 'codex', '-F', '#{pane_dead}'], capture_output=True, text=True, timeout=1) if tmux else None
                if (state / 'exit-code').exists() or check is None or check.returncode or check.stdout.strip() != '0':
                    live_since = None
            except (OSError, subprocess.TimeoutExpired):
                live_since = None
        environment.update(launch['environment'])
        args.cwd = launch['cwd']
        if (state / 'session.json').exists():
            session = json.loads((state / 'session.json').read_text())
            args.session = session['session_id']
            args.cwd = session.get('cwd') or args.cwd
    settings = home / '.config/codex-tools/status.json'
    options = json.loads(settings.read_text()) if settings.exists() else {}
    if args.work_time:
        value = session_work_time(args.session, environment, home, live_since, args.reset_task)
        if value is None:
            raise ValueError('История времени недоступна; укажите --session UUID или --run RUN_ID')
        print(json.dumps(value, ensure_ascii=False) if args.json else work_time_label(value))
        return
    if args.reset_task:
        raise ValueError('--reset-task requires --work-time')
    if args.agents:
        if args.agent_row is not None:
            selected = visible_agent(args.run, home) if args.run else None
            overview = agent_overview(args.session, environment, home, structured=True, selected=selected)
            chosen = next((style.native for _, style in overview if isinstance(style, AgentStyle) and style == 'selected'), None) if isinstance(overview, list) else None
            root = next((style.native for _, style in overview if isinstance(style, AgentStyle) and style in ('main', 'selected')), args.session) if isinstance(overview, list) else args.session
            metrics = session_metrics(chosen or root, environment, home, args.run)
            label = ' Выбранный' if chosen else ' Основной'
            timing = work_time_label(session_work_time(args.session, environment, home, live_since)) if args.agent_row == 2 else ''
            rows = agent_panel_rows(overview, args.width, args.tmux, label + ' · ' + metrics if metrics else '', timing)
            if args.agent_row == 0 and args.tmux and args.run:
                update_agent_height(args.run, len(rows), home)
            print(rows[args.agent_row] if 0 <= args.agent_row < len(rows) else '#[bg=colour236]' if args.tmux else '')
        else:
            text = clean(agent_overview(args.session, environment, home))
            print(text.replace('#', '##') if args.tmux else text)
        return
    value = collect(args.cwd, args.session, environment, home)
    print(json.dumps(value, ensure_ascii=False) if args.json else render(value, options, args.tmux))

if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError) as error:
        print('codex-tools status: ' + clean(error), file=sys.stderr)
        sys.exit(1)
