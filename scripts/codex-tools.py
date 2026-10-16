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

def run(arguments):
    tmux = tmux_path()
    if not tmux:
        raise RuntimeError('tmux is required: brew install tmux, or see docs/tmux.md')
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError('run requires an interactive terminal')
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
