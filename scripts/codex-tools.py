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
            if cwd is not None and pathlib.Path(run['cwd']).resolve() != pathlib.Path(cwd).resolve():
                continue
        except (OSError, RuntimeError):
            continue
        attached = run_status(tmux, run)
        if attached is not None:
            run['attached'] = attached
            runs.append(run)
    return runs


def reuse_resume(tmux, arguments, cwd):
    exact = (len(arguments) == 2 and arguments[0] == 'resume'
             and re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', arguments[1]))
    if arguments != ['resume'] and not exact:
        return None
    runs = live_runs(tmux, None if exact else cwd)
    if exact:
        runs = [run for run in runs if run['session_id'] == arguments[1].lower()]
        if not runs:
            return None
        if len(runs) != 1:
            print('У разговора несколько живых запусков; автоматический выбор невозможен.')
            for run in runs:
                print('  ' + run['directory'].name)
            return 1
        chosen = runs[0]
    else:
        if not runs:
            return None
        print('Живые запуски текущего проекта:')
        for index, run in enumerate(runs, 1):
            state = 'отсоединён' if run['attached'] == 0 else 'подключён другой терминал'
            print(f"{index}. {run['session_id']} — {state}")
        print('0. Открыть обычный список сохранённых разговоров')
        while True:
            try:
                choice = input('Вернуться в запуск (номер; Enter — обычный список): ').strip()
            except (EOFError, KeyboardInterrupt):
                print('\nВозврат отменён.')
                return 0
            if choice in ('', '0'):
                return None
            if choice.isdecimal() and 1 <= int(choice) <= len(runs):
                chosen = runs[int(choice) - 1]
                break
            print('Выберите номер из списка.')
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
    print('Возвращаюсь в работающий Codex: ' + chosen['session_id'])
    return subprocess.call([tmux, '-L', 'codex-tools-' + chosen['directory'].name,
                            '-f', '/dev/null', 'attach-session', '-t', 'codex'])


def run(arguments):
    tmux = tmux_path()
    if not tmux:
        raise RuntimeError('tmux is required: brew install tmux, or see docs/tmux.md')
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError('run requires an interactive terminal')
    reused = reuse_resume(tmux, arguments, os.getcwd())
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
    (directory / 'ready').touch()
    attached = subprocess.call([*prefix, 'attach-session', '-t', 'codex'])
    if (directory / 'exit-code').exists():
        result = int((directory / 'exit-code').read_text())
        if result == 0:
            shutil.rmtree(directory)
        else:
            report_failure(directory, result)
        return result
    print('Detached; return with: ' + shlex.join([tmux, '-L', socket, 'attach']))
    return attached

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
