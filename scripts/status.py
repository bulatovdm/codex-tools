#!/usr/bin/python3
import argparse
import json
import os
import pathlib
import re
import sqlite3
import sys
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
            result['browser'] = 'браузер: CLI проверен' if health.get('healthy') else 'браузер: ошибка сторожа'
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
    args = parser.parse_args()
    home = pathlib.Path.home()
    environment = dict(os.environ)
    if args.run:
        args.session = ''
        if not re.fullmatch(r'[a-f0-9]{32}', args.run):
            raise ValueError('Invalid run identifier')
        state = home / '.local/share/codex-tools/runs' / args.run
        launch = json.loads((state / 'launch.json').read_text())
        environment.update(launch['environment'])
        args.cwd = launch['cwd']
        if (state / 'session.json').exists():
            session = json.loads((state / 'session.json').read_text())
            args.session = session['session_id']
            args.cwd = session.get('cwd') or args.cwd
    settings = home / '.config/codex-tools/status.json'
    options = json.loads(settings.read_text()) if settings.exists() else {}
    value = collect(args.cwd, args.session, environment, home)
    print(json.dumps(value, ensure_ascii=False) if args.json else render(value, options, args.tmux))

if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError) as error:
        print('codex-tools status: ' + clean(error), file=sys.stderr)
        sys.exit(1)
