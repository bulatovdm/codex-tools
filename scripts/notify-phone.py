#!/usr/bin/python3
"""Advisory phone notifications. Never print model context or approval decisions."""
import contextlib
import fcntl
import hashlib
import json
import math
import os
import pathlib
import re
import shlex
import subprocess
import sys
import time
import uuid

KEYS = {'NTFY_TOPIC', 'NTFY_SERVER', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID',
        'IDLE_NOTIFY_SECONDS', 'BACKGROUND_WAIT_LIMIT_SECONDS', 'INPUT_NOTIFY_SECONDS'}
WAITING = '💬 Waiting for your reply'
QUESTION = '❓ Codex asks you a question'
PERMISSION = '🔐 Needs permission'
# Known event listeners and development servers are not awaited jobs. Unknown
# long-running tools are bounded by BACKGROUND_WAIT_LIMIT_SECONDS, never forever.
LISTENER = re.compile(r'listen-events|work-worker\s+listen|http\.server|(?:npm|pnpm|yarn)\s+(?:run\s+)?dev\b')


def codex_home():
    return pathlib.Path(os.environ.get('CODEX_HOME', str(pathlib.Path.home() / '.codex')))


def config_path():
    if os.environ.get('CODEX_NOTIFY_CONFIG'):
        return pathlib.Path(os.environ['CODEX_NOTIFY_CONFIG'])
    own = codex_home() / 'notify.env'
    return own if own.exists() else pathlib.Path.home() / '.claude/notify.env'


def load_config():
    """Read literal KEY=value assignments; never execute a shell config."""
    values = {}
    try:
        for line in config_path().read_text().splitlines():
            words = shlex.split(line, comments=True)
            if words[:1] == ['export']:
                words = words[1:]
            if len(words) == 1 and '=' in words[0]:
                key, value = words[0].split('=', 1)
                if key in KEYS:
                    values[key] = value
    except (OSError, ValueError):
        return {}
    return values


def seconds(config, key, default):
    try:
        value = float(config.get(key, default))
        return value if math.isfinite(value) and value >= 0 else default
    except (TypeError, ValueError):
        return default


def state_directory(session):
    root = pathlib.Path(os.environ.get('CODEX_NOTIFY_STATE_DIR',
                        str(pathlib.Path.home() / '.local/share/codex-tools/notify')))
    directory = root / hashlib.sha256(session.encode()).hexdigest()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


@contextlib.contextmanager
def locked(directory):
    with (directory / 'lock').open('a') as lock:
        os.chmod(directory / 'lock', 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = directory / 'state.json'
        try:
            state = json.loads(path.read_text())
        except (OSError, ValueError):
            state = {}
        yield state
        temporary = directory / ('state-' + uuid.uuid4().hex)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as output:
            json.dump(state, output)
        os.replace(temporary, path)


def transcript_meta(path):
    try:
        with open(path) as stream:
            value = json.loads(stream.readline(1024 * 1024))
        return value.get('payload', {}) if value.get('type') == 'session_meta' else {}
    except (OSError, ValueError, TypeError):
        return {}


def typed_prompt(prompt):
    prompt = prompt.lstrip()
    return bool(prompt) and not prompt.startswith((
        'Work Worker inbox pointer', 'Work Worker automatic startup initialization',
        'Work Worker startup signal',
        '<task-notification', '<cross-session-message', 'Another Claude session sent a message',
        'Message Type: MESSAGE', 'Message Type: FINAL_ANSWER'))


def automatic_approval(path):
    """A request routed to the automatic reviewer is not waiting for a human."""
    context = {}
    try:
        with open(path, 'rb') as stream:
            stream.seek(0, 2)
            offset = max(0, stream.tell() - 2_000_000)
            stream.seek(offset)
            if offset:
                stream.readline()
            for line in stream:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get('type') == 'turn_context':
                    context = entry.get('payload', {})
    except (OSError, TypeError):
        pass
    return context.get('approvals_reviewer') == 'auto_review' or context.get('approval_policy') == 'never'


def tool_fingerprint(event):
    value = event.get('tool_input', {})
    # Approval inputs may add a description that isn't in PostToolUse.
    if event.get('tool_name') in ('Bash', 'exec_command') and isinstance(value, dict):
        value = value.get('command', value.get('cmd', value))
    data = [event.get('tool_name'), value]
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def request_key(event, state):
    identity = [event.get('turn_id') or state.get('prompt'),
                event.get('tool_use_id') or tool_fingerprint(event)]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def git_output(cwd, *arguments):
    try:
        result = subprocess.run(['git', '-C', cwd, 'rev-parse', *arguments],
                                capture_output=True, text=True, timeout=2)
        return result.stdout.strip() if result.returncode == 0 else ''
    except (OSError, subprocess.TimeoutExpired):
        return ''


def project_name(cwd):
    top = git_output(cwd, '--show-toplevel')
    if not top:
        return pathlib.Path(cwd).name or 'session'
    common = git_output(cwd, '--git-common-dir')
    common = pathlib.Path(common)
    if not common.is_absolute():
        common = pathlib.Path(cwd) / common
    main = common.resolve().parent if common.name == '.git' else pathlib.Path(top)
    return main.name + (' (' + pathlib.Path(top).name + ')' if main != pathlib.Path(top) else '')


def session_title(session):
    # The index contains titles only; never derive a title from prompt/message text.
    title = ''
    try:
        with (codex_home() / 'session_index.jsonl').open('rb') as stream:
            stream.seek(0, 2)
            offset = max(0, stream.tell() - 5_000_000)
            stream.seek(offset)
            if offset:
                stream.readline()
            for line in stream:
                try:
                    entry = json.loads(line)
                    if entry.get('id') == session and isinstance(entry.get('thread_name'), str):
                        title = entry['thread_name']
                except ValueError:
                    pass
    except OSError:
        pass
    return ' '.join(title.split())[:160]


def send(config, title, body):
    requests = []
    if config.get('NTFY_TOPIC'):
        payload = {'topic': config['NTFY_TOPIC'], 'title': title, 'message': body, 'priority': 4}
        requests.append(['-H', 'Content-Type: application/json', '--data-binary',
                         json.dumps(payload, ensure_ascii=False), config.get('NTFY_SERVER') or 'https://ntfy.sh'])
    if config.get('TELEGRAM_BOT_TOKEN') and config.get('TELEGRAM_CHAT_ID'):
        requests.append(['--data-urlencode', 'chat_id=' + config['TELEGRAM_CHAT_ID'],
                         '--data-urlencode', 'text=' + title + ' — ' + body,
                         'https://api.telegram.org/bot' + config['TELEGRAM_BOT_TOKEN'] + '/sendMessage'])
    # Independent channels: a failure in ntfy must not prevent Telegram delivery.
    for request in requests:
        try:
            subprocess.run(['curl', '-q', '-fsS', '--max-time', '10', *request],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=12)
        except (OSError, subprocess.TimeoutExpired):
            pass


def title_for(session, state):
    title = 'Codex · ' + project_name(state.get('cwd') or '.')
    name = session_title(session)
    return title + (' · ' + name if name else '')


def transcript_size(path):
    try:
        return os.path.getsize(path)
    except (OSError, TypeError):
        return 0


def resumed(path, offset):
    """Inspect only records appended after Stop; timestamps need not match clocks."""
    try:
        with open(path, 'rb') as stream:
            if os.fstat(stream.fileno()).st_size < offset:
                return True
            stream.seek(offset)
            # Bounded read; unexpectedly large new activity means resumed too.
            data = stream.read(2_000_001)
        if len(data) > 2_000_000:
            return True
        for line in data.splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            payload = entry.get('payload', {})
            if entry.get('type') == 'response_item' and payload.get('type') in (
                    'message', 'function_call', 'custom_tool_call'):
                return True
            if entry.get('type') == 'event_msg' and payload.get('type') in (
                    'task_started', 'turn_started', 'user_message', 'agent_message'):
                return True
    except (OSError, TypeError):
        pass
    return False


def worker(session, token, config):
    directory = state_directory(session)
    while True:
        delay = None
        with locked(directory) as state:
            pending = state.get('pending', {})
            if pending.get('token') != token or state.get('notified') == state.get('prompt'):
                return
            if resumed(pending.get('transcript'), pending.get('offset', 0)):
                state.pop('pending', None)
                return
            now = time.time()
            idle = seconds(config, 'IDLE_NOTIFY_SECONDS', 30)
            limit = seconds(config, 'BACKGROUND_WAIT_LIMIT_SECONDS', 900)
            remaining = pending['at'] + idle - now
            awaited = any(task['at'] >= state.get('prompt_at', now + 1)
                          for task in state.get('tasks', {}).values())
            if remaining > 0:
                delay = min(remaining, 1)
            elif awaited and now < pending['at'] + max(idle, limit):
                delay = min(1, pending['at'] + max(idle, limit) - now)
            else:
                state['notified'] = state['prompt']
                state.pop('pending', None)
                title = title_for(session, state)
                body = WAITING + (' (a background task is still running)' if awaited else '')
        if delay is None:
            send(config, title, body)
            return
        time.sleep(delay)


def launch_worker(session, token):
    subprocess.Popen([sys.executable, str(pathlib.Path(__file__).resolve()), '--worker', session, token],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)


def input_worker(session, key, token, config):
    directory = state_directory(session)
    while True:
        with locked(directory) as state:
            pending = state.get('input_pending', {}).get(key)
            if not pending or pending.get('token') != token:
                return
            # Recheck at delivery time in case the turn context arrived late.
            if automatic_approval(pending.get('transcript')):
                state['input_pending'].pop(key, None)
                return
            remaining = pending['at'] + seconds(config, 'INPUT_NOTIFY_SECONDS', 30) - time.time()
            if remaining <= 0:
                state['input_pending'].pop(key, None)
                title = title_for(session, state)
        if remaining <= 0:
            send(config, title, PERMISSION)
            return
        time.sleep(min(remaining, 1))


def launch_input_worker(session, key, token):
    subprocess.Popen([sys.executable, str(pathlib.Path(__file__).resolve()),
                      '--input-worker', session, key, token],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)


def handle(event, config):
    session = event.get('session_id')
    if not isinstance(session, str) or not session:
        return
    meta = transcript_meta(event.get('transcript_path'))
    source = meta.get('source')
    # exec and subagent rollouts must never notify; CLI and desktop are interactive.
    if source == 'exec' or isinstance(source, dict) or event.get('agent_id'):
        return
    kind = event.get('hook_event_name')
    directory = state_directory(session)
    token = body = input_job = None
    with locked(directory) as state:
        state.setdefault('cwd', meta.get('cwd') or event.get('cwd') or '.')
        if kind == 'UserPromptSubmit' and typed_prompt(event.get('prompt', '')):
            turn = event.get('turn_id')
            if not turn or turn != state.get('prompt_turn'):
                state['prompt'] = uuid.uuid4().hex
                state['prompt_turn'] = turn
                state['prompt_at'] = time.time()
                state.pop('pending', None)
                state['tasks'] = {}
                state['input_pending'] = {}
                state['input_seen'] = []
        elif kind in ('Interrupt', 'SessionEnd'):
            state.pop('pending', None)
            state['tasks'] = {}
            state['input_pending'] = {}
        elif kind in ('PreToolUse', 'PermissionRequest'):
            state.pop('pending', None)
            tool = event.get('tool_name', '')
            if kind == 'PermissionRequest':
                key = request_key(event, state)
                if not automatic_approval(event.get('transcript_path')) and key not in state.get('input_seen', []):
                    job_token = uuid.uuid4().hex
                    state.setdefault('input_seen', []).append(key)
                    state.setdefault('input_pending', {})[key] = {
                        'token': job_token, 'at': time.time(),
                        'fingerprint': tool_fingerprint(event),
                        'call_id': event.get('tool_use_id'),
                        'transcript': event.get('transcript_path')}
                    input_job = (session, key, job_token)
            elif re.search(r'(?:^|__|\.)(?:request_user_input|request_user_input_async)$', tool):
                key = request_key(event, state)
                if key not in state.get('input_seen', []):
                    state.setdefault('input_seen', []).append(key)
                    body = QUESTION
            if kind == 'PreToolUse' and tool in ('Bash', 'exec_command') and state.get('prompt'):
                command = event.get('tool_input', {}).get('command', event.get('tool_input', {}).get('cmd', ''))
                call = event.get('tool_use_id')
                if call and not LISTENER.search(command):
                    state.setdefault('tasks', {})[call] = {'at': time.time()}
        elif kind == 'PostToolUse':
            state.get('tasks', {}).pop(event.get('tool_use_id'), None)
            fingerprint = tool_fingerprint(event)
            for key, request in list(state.get('input_pending', {}).items()):
                matches = (request['call_id'] == event.get('tool_use_id') if request.get('call_id')
                           else request['fingerprint'] == fingerprint)
                if matches:
                    state['input_pending'].pop(key, None)
        elif kind == 'Stop':
            state['input_pending'] = {}
            if state.get('prompt') and state.get('notified') != state['prompt']:
                token = uuid.uuid4().hex
                path = event.get('transcript_path')
                state['pending'] = {'token': token, 'at': time.time(), 'transcript': path,
                                    'offset': transcript_size(path)}
        title = title_for(session, state) if body else None
    if token:
        launch_worker(session, token)
    if input_job:
        launch_input_worker(*input_job)
    if body:
        # The network is also detached for approval/question hooks.
        subprocess.Popen([sys.executable, str(pathlib.Path(__file__).resolve()), '--send', title, body],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)


def main():
    os.umask(0o077)
    event = None
    try:
        config = load_config()
        if sys.argv[1:2] == ['--worker']:
            if config:
                worker(sys.argv[2], sys.argv[3], config)
        elif sys.argv[1:2] == ['--input-worker']:
            if config:
                input_worker(sys.argv[2], sys.argv[3], sys.argv[4], config)
        elif sys.argv[1:2] == ['--send']:
            if config:
                send(config, sys.argv[2], sys.argv[3])
        else:
            event = json.load(sys.stdin)
            if config:
                handle(event, config)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        # Notification failures must not interrupt Codex or expose private inputs.
        pass
    finally:
        if isinstance(event, dict) and event.get('hook_event_name') == 'Stop':
            print('{}')


if __name__ == '__main__':
    main()
