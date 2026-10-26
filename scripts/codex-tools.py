#!/usr/bin/python3
import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import uuid
import re
import sqlite3
import signal

ROOT = pathlib.Path(__file__).resolve().parent

WORK_WORKER_STARTUP_PROMPT = (
    "Work Worker automatic startup initialization. This is a service turn, not a development task. "
    "Let the trusted native hooks initialize this new session. Do not use tools, run commands, "
    "edit files or settings, send mail, or start further tasks. Reply only WW_SESSION_READY."
)


def startup_arguments(arguments, cwd):
    try:
        return _startup_arguments(arguments, cwd)
    except (OSError, RuntimeError):
        return arguments


def _startup_arguments(arguments, cwd):
    # Recognize only empty native launches whose argument meaning is known. All
    # prompts, subcommands, config/profile overrides and future flags pass through.
    selected = pathlib.Path(cwd).resolve()
    valued = {'-C', '--cd', '-m', '--model', '-s', '--sandbox', '-a', '--ask-for-approval', '--add-dir'}
    flags = {'--no-alt-screen', '--oss', '--search', '--strict-config', '--approve-for-me'}
    index = 0
    while index < len(arguments):
        arg = arguments[index]
        if arg in valued:
            if index + 1 >= len(arguments) or arguments[index + 1].startswith('-'):
                return arguments
            if arg in {'-C', '--cd'}:
                selected = (pathlib.Path(cwd) / arguments[index + 1]).resolve()
            index += 2
        elif any(arg.startswith(option + '=') for option in valued if option.startswith('--')):
            option, value = arg.split('=', 1)
            if not value:
                return arguments
            if option == '--cd':
                selected = (pathlib.Path(cwd) / value).resolve()
            index += 1
        elif arg in flags:
            index += 1
        else:
            return arguments
    executable = shutil.which('work-worker')
    if not executable:
        return arguments
    try:
        result = subprocess.run([executable, 'startup-context'], cwd=str(selected),
                                capture_output=True, text=True, timeout=1.5)
        if result.returncode != 0 or len(result.stdout) > 4096:
            return arguments
        context = json.loads(result.stdout)
        if not isinstance(context, dict) or context.get('version') != 1 or context.get('enabled') is not True:
            return arguments
        root = pathlib.Path(context['project_root'])
        if not root.is_absolute() or not selected.is_relative_to(root.resolve()):
            return arguments
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        return arguments
    return [*arguments, '--', WORK_WORKER_STARTUP_PROMPT]


def tmux_path():
    installed = pathlib.Path.home() / '.local/share/codex-tools/tmux/bin/tmux'
    return str(installed) if installed.exists() else shutil.which('tmux')

def report_failure(directory, result):
    print('codex-tools: Codex завершился с кодом ' + str(result) + '.')
    try:
        with (directory / 'terminal-error.log').open(encoding='utf-8', errors='replace') as source:
            text = source.read(65536)
        text = re.sub(r'\x1b\][^\x07]*(?:\x07|\x1b\\)', '', text)
        text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
        text = re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', text)
        error = next((line.strip() for line in text.splitlines()
                      if re.match(r'^\s*(?:error|fatal):', line, re.IGNORECASE)), '')[:500]
    except OSError:
        error = ''
    missing = re.search(r'No saved session found with ID ([A-Za-z0-9_-]+)', error)
    if missing:
        session = missing.group(1)
        print('Сохранённая сессия ' + session + ' не найдена.')
        if re.fullmatch(r'[0-9a-fA-F-]+', session) and session.count('-') == 4 and len(session) != 36:
            print('ID похож на UUID, но содержит ' + str(len(session)) + ' символов вместо 36. Скопируйте его целиком.')
        print('Проверьте ID или выберите сохранённую сессию командой: cx resume')
    elif error:
        print('Причина: ' + re.sub(r'^(?:error|fatal):\s*', '', error, flags=re.IGNORECASE))
    else:
        print('Причину не удалось прочитать автоматически; откройте terminal-error.log в каталоге диагностики.')
    print('Диагностика: ' + str(directory))

def read_run(directory):
    """Use only private hook identity; never infer a thread from cwd/history."""
    if directory.is_symlink() or not re.fullmatch(r'[a-f0-9]{32}', directory.name):
        return None
    try:
        if (directory / 'exit-code').exists():
            return None
        values = []
        for name in ('session.json', 'launch.json'):
            path = directory / name
            if path.is_symlink() or path.stat().st_size > 65536:
                return None
            value = json.loads(path.read_text())
            if not isinstance(value, dict):
                return None
            values.append(value)
        session, launch = values
        native = session.get('session_id')
        if not isinstance(native, str) or str(uuid.UUID(native)) != native.lower():
            return None
        cwd = session.get('cwd')
        if not isinstance(cwd, str) or not pathlib.Path(cwd).is_absolute():
            return None
        if launch.get('cli') != str(pathlib.Path.home() / '.local/bin/codex'):
            return None
        return {'directory': directory, 'session_id': native.lower(), 'cwd': cwd}
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def run_status(tmux, run):
    directory = run['directory']
    prefix = [tmux, '-L', 'codex-tools-' + directory.name, '-f', '/dev/null']
    try:
        result = subprocess.run([*prefix, 'list-panes', '-t', 'codex', '-F',
                                 '#{session_attached}\t#{pane_dead}\t#{pane_start_command}'],
                                capture_output=True, text=True, timeout=1.5)
        lines = result.stdout.splitlines()
        if result.returncode != 0 or len(lines) != 1:
            return None
        attached, dead, command = lines[0].split('\t', 2)
        expected = ['/usr/bin/python3', str(ROOT / 'launch.py'), str(directory)]
        parts = shlex.split(command)
        if len(parts) == 1:
            parts = shlex.split(parts[0])
        if dead != '0' or not attached.isdecimal() or parts != expected:
            return None
        return int(attached)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def live_runs(tmux, cwd=None):
    root = pathlib.Path.home() / '.local/share/codex-tools/runs'
    if root.is_symlink():
        return []
    runs = []
    try:
        directories = sorted(root.iterdir())
    except OSError:
        return runs
    for directory in directories:
        run = read_run(directory)
        if run is None:
            continue
        try:
            if cwd is not None and project_directory(run['cwd']) != project_directory(cwd):
                continue
        except (OSError, RuntimeError):
            continue
        attached = run_status(tmux, run)
        if attached is not None:
            run['attached'] = attached
            runs.append(run)
    return runs


def project_directory(cwd):
    path = pathlib.Path(cwd).resolve()
    return next((parent for parent in (path, *path.parents) if (parent / '.git').exists()), path)


def resume_request(arguments, cwd):
    """Parse selectors, without interpreting user prompts or executing config."""
    valued = {'-C', '--cd', '-m', '--model', '-p', '--profile', '-c', '--config',
              '-s', '--sandbox', '-a', '--ask-for-approval', '--add-dir',
              '--enable', '--disable', '--local-provider', '--remote', '--remote-auth-token-env', '-i', '--image'}
    flags = {'--last', '--all', '--include-non-interactive', '--no-alt-screen', '--no-daemon',
             '--search', '--oss', '--strict-config', '--worktree', '--approve-for-me',
             '--dangerously-bypass-approvals-and-sandbox', '--dangerously-bypass-hook-trust'}
    request = {'cwd': cwd, 'target': None, 'all': False, 'last': False, 'overrides': False}
    found = False
    index = 0
    while index < len(arguments):
        arg = arguments[index]
        option, equal, value = arg.partition('=')
        if option in ('--remote', '--remote-auth-token-env', '--worktree'):
            return None
        if option in valued:
            if not equal:
                index += 1
                if index >= len(arguments):
                    return None
                value = arguments[index]
            if not value or value.startswith('-'):
                return None
            if option in ('-C', '--cd'):
                request['cwd'] = str(pathlib.Path(cwd) / value)
            else:
                request['overrides'] = True
        elif arg in flags:
            if arg in ('--all', '--last'):
                if not found:
                    return None
                request[arg[2:]] = True
            elif arg not in ('--include-non-interactive', '--no-alt-screen', '--no-daemon'):
                request['overrides'] = True
        elif arg.startswith('-'):
            return None
        elif not found:
            if arg != 'resume':
                return None
            found = True
        elif request['target'] is None:
            request['target'] = arg
        else:
            # A follow-up prompt cannot silently be discarded when reattaching.
            request['overrides'] = True
        index += 1
    return request if found else None


def run_titles(runs):
    ids = {run['session_id'] for run in runs}
    titles = {}
    home = pathlib.Path(os.environ.get('CODEX_HOME', str(pathlib.Path.home() / '.codex')))
    try:
        with (home / 'session_index.jsonl').open('rb') as source:
            source.seek(0, 2)
            offset = max(0, source.tell() - 5_000_000)
            source.seek(offset)
            if offset:
                source.readline()
            for line in source:
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if (isinstance(value, dict) and isinstance(value.get('id'), str)
                        and value['id'] in ids and isinstance(value.get('thread_name'), str)):
                    titles[value['id']] = value['thread_name']
    except OSError:
        pass
    return titles


def saved_sessions(cwd, all_projects, include_non_interactive=False):
    """Read the native local session catalog; never inspect conversation bodies."""
    database = pathlib.Path(os.environ.get('CODEX_HOME', str(pathlib.Path.home() / '.codex'))) / 'state_5.sqlite'
    if not database.is_file():
        return []
    try:
        with sqlite3.connect('file:' + str(database) + '?mode=ro', uri=True, timeout=0.5) as connection:
            sources = ('cli', 'vscode', 'exec') if include_non_interactive else ('cli', 'vscode')
            rows = connection.execute(
                'SELECT id, cwd, name, title, recency_at_ms, rollout_path FROM threads '
                'WHERE archived = 0 AND source IN (' + ','.join('?' for _ in sources) + ') '
                'ORDER BY recency_at_ms DESC LIMIT 1000', sources).fetchall()
    except (OSError, sqlite3.Error):
        return []
    sessions = []
    for native, directory, name, title, updated, rollout in rows:
        try:
            if not isinstance(native, str) or str(uuid.UUID(native)) != native.lower():
                continue
            if not isinstance(directory, str) or not pathlib.Path(directory).is_absolute():
                continue
            if not all_projects and project_directory(directory) != project_directory(cwd):
                continue
            if not isinstance(rollout, str) or not pathlib.Path(rollout).is_file():
                continue
        except (ValueError, TypeError, OSError, RuntimeError):
            continue
        sessions.append({'session_id': native.lower(), 'cwd': directory,
                         'name': name if isinstance(name, str) else '',
                         'title': title if isinstance(title, str) else '',
                         'updated': updated if isinstance(updated, int) else 0})
    return sessions


def display_title(session, fallback=None):
    title = ''
    for candidate in (session.get('name'), fallback, session.get('title')):
        if isinstance(candidate, str):
            title = re.sub(r'[\x00-\x1f\x7f]+', ' ', candidate).strip()
            if title:
                break
    title = title or 'Без названия'
    return (title[:87] + '…') if len(title) > 88 else title


def selected_resume_arguments(arguments, native):
    selected = [argument for argument in arguments if argument not in ('--last', '--all')]
    index = selected.index('resume')
    selected.insert(index + 1, native)
    return selected


def configure_lifecycle(tmux, directory):
    prefix = [tmux, '-L', 'codex-tools-' + directory.name, '-f', '/dev/null']
    # Arm only on attach: a freshly created detached server must survive setup.
    subprocess.run([*prefix, 'set-hook', '-g', 'client-attached',
                    'set-option -t codex destroy-unattached on'], check=True)
    # Deliberate prefix+d preserves the run; closing the terminal does not.
    subprocess.run([*prefix, 'bind-key', 'd', 'set-option', '-t', 'codex',
                    'destroy-unattached', 'off', '\\;', 'detach-client'], check=True)


def attach_with_close_marker(tmux, directory):
    """Record a terminal hangup before tmux destroys this private run."""
    previous = signal.getsignal(signal.SIGHUP)

    def terminal_closed(signum, frame):
        try:
            descriptor = os.open(directory / 'normal-close',
                                 os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
        except OSError:
            pass

    prefix = [tmux, '-L', 'codex-tools-' + directory.name, '-f', '/dev/null']
    signal.signal(signal.SIGHUP, terminal_closed)
    try:
        return subprocess.call([*prefix, 'attach-session', '-t', 'codex'])
    finally:
        signal.signal(signal.SIGHUP, previous)


def finish_attachment(directory, attached, tmux):
    if (directory / 'exit-code').exists():
        result = int((directory / 'exit-code').read_text())
        if result == 0:
            shutil.rmtree(directory, ignore_errors=True)
        else:
            report_failure(directory, result)
        return result
    run = read_run(directory)
    if run is not None and run_status(tmux, run) is not None:
        print('Codex сохранён в фоне. Вернуться: cx resume ' + run['session_id'])
    else:
        print('Подключение завершено; живой запуск не подтверждён. Диагностика: ' + str(directory))
    return attached


def reuse_resume(tmux, arguments, cwd):
    request = resume_request(arguments, cwd)
    if request is None:
        return None
    target = request['target']
    runs = live_runs(tmux, None if target or request['all'] else request['cwd'])
    if target:
        exact = re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', target)
        titles = {} if exact else run_titles(runs)
        runs = [run for run in runs if run['session_id'] == target.lower()] if exact else [
            run for run in runs if titles.get(run['session_id']) == target]
        if not runs:
            return None
        if request['overrides']:
            print('Разговор уже работает. Новые параметры или prompt нельзя применить через attach.')
            print('Вернитесь без overrides: cx resume ' + runs[0]['session_id'])
            print('Для полного завершения: /quit или /exit внутри этого Codex.')
            return 1
        if len(runs) != 1:
            print('У разговора несколько живых запусков; автоматический выбор невозможен.')
            for run in runs:
                print('  ' + run['directory'].name)
            return 1
        chosen = runs[0]
    else:
        saved = saved_sessions(request['cwd'], request['all'],
                               '--include-non-interactive' in arguments)
        live_ids = {run['session_id'] for run in runs}
        entries = [dict(run, kind='live') for run in runs]
        entries.extend(dict(session, kind='saved') for session in saved
                       if session['session_id'] not in live_ids)
        if not entries:
            return None
        names = run_titles(runs)
        saved_by_id = {session['session_id']: session for session in saved}
        print('Разговоры всех проектов:' if request['all'] else 'Разговоры текущего проекта:')
        for index, entry in enumerate(entries, 1):
            if entry['kind'] == 'saved':
                state = 'в истории'
            else:
                state = 'работает в фоне' if entry['attached'] == 0 else 'открыта в другом терминале'
            details = saved_by_id.get(entry['session_id'], entry)
            title = display_title(details, names.get(entry['session_id']))
            project = project_directory(entry['cwd'])
            print(f'{index}. {title} — {project} — {state}')
        print('0. Отмена')
        while True:
            try:
                choice = input('Открыть разговор (номер; Enter — отмена): ').strip()
            except (EOFError, KeyboardInterrupt):
                print('\nВозврат отменён.')
                return 0
            if choice in ('', '0'):
                print('Возврат отменён.')
                return 0
            if choice.isdecimal() and 1 <= int(choice) <= len(entries):
                chosen = entries[int(choice) - 1]
                break
            print('Выберите номер из списка.')
        if chosen['kind'] == 'saved':
            # The run wrapper starts exactly this saved conversation. It must
            # never launch the native picker again after the user chose an ID.
            if any(run['session_id'] == chosen['session_id'] for run in live_runs(tmux, None)):
                print('Разговор стал работающим; повторите cx resume.')
                return 1
            print('Открываю сохранённый разговор: ' + chosen['session_id'])
            return ('native', selected_resume_arguments(arguments, chosen['session_id']))
        if sum(run['session_id'] == chosen['session_id'] for run in runs) != 1:
            print('У разговора несколько живых запусков; автоматический выбор невозможен.')
            return 1
        if request['overrides']:
            print('Выбранный разговор уже работает; новые параметры/prompt не применены.')
            print('Вернитесь без overrides: cx resume ' + chosen['session_id'])
            return 1
    # Recheck hook identity and liveness after the interactive selection.
    current = read_run(chosen['directory'])
    if current != {key: chosen[key] for key in ('directory', 'session_id', 'cwd')}:
        print('Запуск изменился или завершился; повторите cx resume.')
        return 1
    attached = run_status(tmux, current)
    if attached is None:
        print('Запуск уже недоступен; повторите cx resume.')
        return 1
    if attached:
        print('Этот разговор уже открыт: к запуску подключён другой терминал.')
        return 1
    configure_lifecycle(tmux, chosen['directory'])
    print('Возвращаюсь в работающий Codex: ' + chosen['session_id'])
    result = attach_with_close_marker(tmux, chosen['directory'])
    return finish_attachment(chosen['directory'], result, tmux)


def run(arguments):
    tmux = tmux_path()
    if not tmux:
        raise RuntimeError('tmux is required: brew install tmux, or see docs/tmux.md')
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError('run requires an interactive terminal')
    reused = reuse_resume(tmux, arguments, os.getcwd())
    if isinstance(reused, tuple) and reused[0] == 'native':
        arguments = reused[1]
        reused = None
    if reused is not None:
        return reused
    identity = uuid.uuid4().hex
    directory = pathlib.Path.home() / '.local/share/codex-tools/runs' / identity
    directory.mkdir(parents=True, mode=0o700)
    directory.chmod(0o700)
    cli = pathlib.Path.home() / '.local/bin/codex'
    if not cli.exists():
        raise RuntimeError('Codex CLI is missing: ' + str(cli))
    environment = {key: value for key, value in os.environ.items() if key in ['EMPIRIKA_BASE_URL', 'EMPIRIKA_EVENT_PROJECT', 'EMPIRIKA_EVENT_ASSET', 'EMPIRIKA_LISTENERS_DIR', 'WORK_WORKER_HOME']}
    arguments = startup_arguments(arguments, os.getcwd())
    launch = {'cwd': os.getcwd(), 'args': arguments, 'cli': str(cli), 'environment': environment}
    descriptor = os.open(directory / 'launch.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as output:
        json.dump(launch, output)
    socket = 'codex-tools-' + identity
    prefix = [tmux, '-L', socket, '-f', '/dev/null']
    subprocess.run([*prefix, 'new-session', '-d', '-s', 'codex', '-c', os.getcwd(), shlex.join(['/usr/bin/python3', str(ROOT / 'launch.py'), str(directory)])], check=True)
    command = shlex.join(['/usr/bin/python3', str(ROOT / 'status.py'), '--run', identity, '--tmux'])
    settings = {'status': 'on', 'status-position': 'bottom', 'status-interval': '2', 'status-format[0]': '#(' + command + ')', 'status-style': 'fg=colour250,bg=colour235', 'mouse': 'on', 'exit-empty': 'on'}
    for key, value in settings.items():
        subprocess.run([*prefix, 'set-option', '-g', key, value], check=True)
    configure_lifecycle(tmux, directory)
    print('Ctrl-C прерывает работу; на пустом вводе выходит. /quit — полный выход.')
    print('Закрытие окна завершает запуск. Ctrl-B, затем D — сохранить в фоне.')
    sys.stdout.flush()
    (directory / 'ready').touch()
    attached = attach_with_close_marker(tmux, directory)
    return finish_attachment(directory, attached, tmux)

def main():
    args = sys.argv[1:]
    if not args or args[0] in ['help', '--help', '-h']:
        print('codex-tools run [Codex arguments]\ncodex-tools status [--session ID] [--json]\ncodex-tools doctor\ncodex-tools browser\ncodex-tools history [--apply] [--config PATH]')
        return 0
    if args[0] == 'run':
        return run(args[1:])
    if args[0] == 'status':
        return subprocess.call(['/usr/bin/python3', str(ROOT / 'status.py'), *args[1:]])
    if args[0] == 'history':
        return subprocess.call(['python3', str(ROOT / 'history.py'), *args[1:]])
    if args[0] == 'browser':
        return subprocess.call([str(pathlib.Path.home() / '.local/bin/codex-browser-guard'), *args[1:]])
    if args[0] == 'doctor':
        print('tmux: ' + (tmux_path() or 'missing'))
        print('session hook: trust codex-tools entries in /hooks')
        return subprocess.call([str(pathlib.Path.home() / '.local/bin/codex-browser-guard')])
    raise RuntimeError('Unknown command: ' + args[0])

if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print('codex-tools: ' + str(error), file=sys.stderr)
        sys.exit(1)
