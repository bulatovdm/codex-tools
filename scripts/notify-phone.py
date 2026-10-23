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
STOPPED = '⚠️ Codex stopped unexpectedly'
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


def automatic_approval(path, state=None):
    """A request routed to the automatic reviewer is not waiting for a human."""
    cache = (state or {}).get('approval_context', {})
    context = {}
    try:
        with open(path, 'rb') as stream:
            info = os.fstat(stream.fileno())
            identity = [str(path), info.st_dev, info.st_ino]
            offset = 0
            if cache.get('file') == identity and cache.get('offset', 0) <= info.st_size:
                offset = cache['offset']
                context = cache.get('context', {})
            stream.seek(offset)
            while True:
                line = stream.readline()
                if not line or not line.endswith(b'\n'):
                    break
                offset = stream.tell()
                # Skip large tool outputs without decoding their contents.
                if not re.search(rb'"type"\s*:\s*"turn_context"', line[:512]):
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get('type') == 'turn_context':
                    payload = entry.get('payload', {})
                    context = {key: payload.get(key) for key in ('approvals_reviewer', 'approval_policy')}
            if state is not None:
                state['approval_context'] = {'file': identity, 'offset': offset, 'context': context}
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


def channels(config):
    return ({'ntfy'} if config.get('NTFY_TOPIC') else set()) | (
        {'telegram'} if config.get('TELEGRAM_BOT_TOKEN') and config.get('TELEGRAM_CHAT_ID') else set())


def send(config, title, body, skip=(), notification_id=None):
    requests = []
    if config.get('NTFY_TOPIC') and 'ntfy' not in skip:
        payload = {'topic': config['NTFY_TOPIC'], 'title': title, 'message': body, 'priority': 4}
        if notification_id:
            payload['sequence_id'] = notification_id
        requests.append(('ntfy', ['-H', 'Content-Type: application/json', '--data-binary',
                         json.dumps(payload, ensure_ascii=False), config.get('NTFY_SERVER') or 'https://ntfy.sh']))
    if config.get('TELEGRAM_BOT_TOKEN') and config.get('TELEGRAM_CHAT_ID') and 'telegram' not in skip:
        requests.append(('telegram', ['--data-urlencode', 'chat_id=' + config['TELEGRAM_CHAT_ID'],
                         '--data-urlencode', 'text=' + title + ' — ' + body,
                         'https://api.telegram.org/bot' + config['TELEGRAM_BOT_TOKEN'] + '/sendMessage']))
    # Independent channels: a failure in ntfy must not prevent Telegram delivery.
    accepted = set()
    for channel, request in requests:
        try:
            result = subprocess.run(['curl', '-q', '-fsS', '--max-time', '10', *request],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=12)
            if result.returncode == 0:
                accepted.add(channel)
        except (OSError, subprocess.TimeoutExpired):
            pass
    return accepted


def queue_delivery(state, key, body, guard):
    record = state.setdefault('deliveries', {}).setdefault(key, {'done': [], 'attempts': 0})
    record.update(body=body, guard=guard)
    if record.get('status') != 'delivered':
        record['status'] = 'pending'


def delivery_current(state, guard):
    kind = guard['kind']
    if kind == 'waiting':
        return (state.get('prompt') == guard['prompt'] and state.get('notified') != guard['prompt']
                and state.get('pending', {}).get('token') == guard['token'] and not state.get('questions'))
    if kind == 'question':
        return guard['key'] in state.get('questions', {})
    if kind == 'permission':
        return state.get('input_pending', {}).get(guard['key'], {}).get('token') == guard['token']
    return state.get('failure_token') == guard['token']


def delivery_worker(session, key, config):
    directory = state_directory(session)
    owner = uuid.uuid4().hex
    while True:
        pause = 1
        with locked(directory) as state:
            record = state.get('deliveries', {}).get(key)
            if not record or record.get('status') == 'delivered':
                return
            if not config_path().exists():
                record['status'] = 'disabled'
                return
            guard = record['guard']
            current = delivery_current(state, guard)
            if guard['kind'] == 'waiting':
                pending = state.get('pending', {})
                current = current and not resumed(pending.get('transcript'), pending.get('offset', 0))
            if guard['kind'] == 'permission':
                request = state.get('input_pending', {}).get(guard['key'], {})
                current = current and not automatic_approval(request.get('transcript'), state)
            if not current:
                record['status'] = 'cancelled'
                return
            now = time.time()
            lease = record.get('lease', {})
            due = max(record.get('not_before', 0), record.get('retry_at', 0),
                      lease.get('until', 0) if lease.get('owner') != owner else 0)
            if due > now:
                pause = min(due - now, 1)
                ready = False
            else:
                record['lease'] = {'owner': owner, 'until': now + 30}
                done = set(record['done'])
                body = record['body']
                title = title_for(session, state)
                ready = True
        if not ready:
            time.sleep(pause)
            continue
        notification_id = 'codex-' + hashlib.sha256((session + ':' + key).encode()).hexdigest()[:32]
        accepted = send(config, title, body, skip=done, notification_id=notification_id)
        with locked(directory) as state:
            record = state.get('deliveries', {}).get(key)
            if not record or record.get('lease', {}).get('owner') != owner:
                return
            record['done'] = sorted(set(record['done']) | accepted)
            record.pop('lease', None)
            record['attempts'] += 1
            if channels(config).issubset(record['done']):
                record['status'] = 'delivered'
                if record['guard']['kind'] == 'waiting' and state.get('prompt') == record['guard']['prompt']:
                    state['notified'] = state['prompt']
                    state.pop('pending', None)
                return
            record['retry_at'] = time.time() + min(60, 5 * 2 ** min(record['attempts'] - 1, 4))


def launch_delivery_worker(session, key):
    subprocess.Popen([sys.executable, str(pathlib.Path(__file__).resolve()), '--deliver', session, key],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)


def title_for(session, state):
    title = 'Codex · ' + project_name(state.get('cwd') or '.')
    name = session_title(session)
    short = session[-8:] if re.fullmatch(r'[a-fA-F0-9-]{36}', session) else hashlib.sha256(session.encode()).hexdigest()[:8]
    return title + (' · ' + name if name else '') + ' [' + short + ']'


def running_session(response):
    """Read supported unified-exec results; never retain output text."""
    if isinstance(response, dict):
        value = response.get('session_id')
        if value is not None and (response.get('exit_code') is None):
            return str(value)
        for content in response.get('content', []):
            if isinstance(content, dict):
                value = running_session(content.get('text'))
                if value:
                    return value
    if isinstance(response, str):
        try:
            decoded = json.loads(response)
        except ValueError:
            decoded = None
        if isinstance(decoded, dict):
            return running_session(decoded)
        match = re.search(r'(?:Process running with session ID|Session ID:)\s*(\d+)', response)
        if match and not re.search(r'(?:Process exited|exit_code"?\s*:\s*\d)', response):
            return match.group(1)
    return None


def is_question(tool):
    return bool(re.search(r'(?:^|__|\.)(?:request_user_input|request_user_input_async)$', tool))


def tool_failed(response):
    if isinstance(response, str):
        try:
            return tool_failed(json.loads(response))
        except ValueError:
            return bool(re.search(r'(?i)(?:unavailable|not available|cancelled|canceled|failed|error)', response))
    return isinstance(response, dict) and (response.get('isError') is True or response.get('accepted') is False or bool(response.get('error')))


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
            if state.get('questions'):
                state.pop('pending', None)
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
                body = WAITING + (' (a background task is still running)' if awaited else '')
                key = 'waiting:' + state['prompt']
                queue_delivery(state, key, body, {'kind': 'waiting', 'prompt': state['prompt'], 'token': token})
        if delay is None:
            delivery_worker(session, key, config)
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
            if automatic_approval(pending.get('transcript'), state):
                state['input_pending'].pop(key, None)
                return
            remaining = pending['at'] + seconds(config, 'INPUT_NOTIFY_SECONDS', 30) - time.time()
            if remaining <= 0:
                delivery_key = 'permission:' + key
                queue_delivery(state, delivery_key, PERMISSION, {'kind': 'permission', 'key': key, 'token': token})
        if remaining <= 0:
            delivery_worker(session, delivery_key, config)
            return
        time.sleep(min(remaining, 1))


def launch_input_worker(session, key, token):
    subprocess.Popen([sys.executable, str(pathlib.Path(__file__).resolve()),
                      '--input-worker', session, key, token],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)


def process_identity(pid):
    try:
        result = subprocess.run(['ps', '-o', 'lstart=', '-p', str(pid)],
                                capture_output=True, text=True, timeout=1)
        return result.stdout.strip() if result.returncode == 0 else ''
    except (OSError, subprocess.TimeoutExpired):
        return None  # An observation error is not evidence of a process exit.


def find_owner():
    pid = os.getppid()
    deadline = time.monotonic() + 1.5
    for _ in range(12):
        if pid <= 1 or time.monotonic() > deadline:
            break
        try:
            fields = []
            for field in ('ppid=', 'comm=', 'args='):
                result = subprocess.run(['ps', '-ww', '-o', field, '-p', str(pid)],
                                        capture_output=True, text=True, timeout=0.3)
                if result.returncode:
                    return None
                fields.append(result.stdout.strip())
            parent, command, arguments = fields
            name = pathlib.Path(command).name
            if name in ('codex', 'codex-aarch64-apple-darwin', 'codex-x86_64-apple-darwin') and not re.search(
                    r'\bcodex\S*\s+(?:app-server|exec-server|exec|review|remote-control)\b', arguments):
                started = process_identity(pid)
                if started:
                    return {'pid': pid, 'started': started, 'token': uuid.uuid4().hex}
            pid = int(parent)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            break
    return None


def queue_failure(state, token, code=None):
    state['failure_token'] = token
    state.pop('pending', None)
    state['questions'] = {}
    state['input_pending'] = {}
    key = 'failure:' + token
    body = STOPPED + (' (exit code ' + str(code) + ')' if code is not None else '')
    queue_delivery(state, key, body, {'kind': 'failure', 'token': token})
    return key


def watch_owner(session, token, config):
    directory = state_directory(session)
    while True:
        with locked(directory) as state:
            owner = state.get('owner', {})
            if owner.get('token') != token or state.get('ended'):
                return
        identity = process_identity(owner['pid'])
        if identity is not None and identity != owner['started']:
            # Give the cx parent time to report a normal exit / Ctrl-C. Also
            # confirm disappearance rather than treating one failed ps as death.
            time.sleep(2)
            confirmed = process_identity(owner['pid'])
            if confirmed is None or confirmed == owner['started']:
                continue
            with locked(directory) as state:
                if state.get('owner', {}).get('token') != token or state.get('ended'):
                    return
                key = queue_failure(state, token)
                state['ended'] = True
            delivery_worker(session, key, config)
            return
        time.sleep(5)


def launch_owner_watcher(session, token):
    subprocess.Popen([sys.executable, str(pathlib.Path(__file__).resolve()), '--watch', session, token],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)


def report_exit(directory, code, config):
    """The cx parent knows the actual exit status, including failures with SessionEnd."""
    directory = pathlib.Path(directory)
    launch = json.loads((directory / 'launch.json').read_text())
    try:
        captured = json.loads((directory / 'session.json').read_text())
    except (OSError, ValueError):
        captured = {}
    session = captured.get('session_id') or 'launch-' + directory.name
    with locked(state_directory(session)) as state:
        state.setdefault('cwd', captured.get('cwd') or launch['cwd'])
        state['ended'] = True
        if code in (0, 130, -2):
            state.pop('failure_token', None)
            state.pop('pending', None)
            state['input_pending'] = {}
            state['questions'] = {}
            return
        token = state.get('owner', {}).get('token') or directory.name
        key = queue_failure(state, token, code)
    launch_delivery_worker(session, key)


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
    token = delivery_key = input_job = owner_job = None
    with locked(directory) as state:
        state.setdefault('cwd', meta.get('cwd') or event.get('cwd') or '.')
        if kind == 'SessionStart':
            state['ended'] = False
            state.pop('failure_token', None)
        if source == 'cli' and (meta.get('id') or meta.get('session_id')) == session and (
                kind == 'SessionStart' or not state.get('owner_attempted')):
            state['owner_attempted'] = True
            owner = find_owner()
            if owner:
                state['owner'] = owner
                state['ended'] = False
                owner_job = (session, owner['token'])
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
                state['questions'] = {}
                state['deliveries'] = {}
                state.pop('failure_token', None)
                automatic_approval(event.get('transcript_path'), state)
        elif kind in ('Interrupt', 'SessionEnd'):
            state.pop('pending', None)
            state['tasks'] = {}
            state['input_pending'] = {}
            state['questions'] = {}
            state['ended'] = kind == 'SessionEnd'
        elif kind in ('PreToolUse', 'PermissionRequest'):
            state.pop('pending', None)
            tool = event.get('tool_name', '')
            if kind == 'PermissionRequest':
                key = request_key(event, state)
                if not automatic_approval(event.get('transcript_path'), state) and key not in state.get('input_seen', []):
                    job_token = uuid.uuid4().hex
                    state.setdefault('input_seen', []).append(key)
                    state.setdefault('input_pending', {})[key] = {
                        'token': job_token, 'at': time.time(),
                        'fingerprint': tool_fingerprint(event),
                        'call_id': event.get('tool_use_id'),
                        'transcript': event.get('transcript_path')}
                    input_job = (session, key, job_token)
            elif is_question(tool):
                key = request_key(event, state)
                if key not in state.get('input_seen', []):
                    state.setdefault('input_seen', []).append(key)
                    state.setdefault('questions', {})[key] = {
                        'call_id': event.get('tool_use_id'), 'fingerprint': tool_fingerprint(event),
                        'async': tool.endswith('request_user_input_async')}
                    delivery_key = 'question:' + key
                    queue_delivery(state, delivery_key, QUESTION, {'kind': 'question', 'key': key})
                    state['deliveries'][delivery_key]['not_before'] = time.time() + seconds(config, 'INPUT_NOTIFY_SECONDS', 30)
            if kind == 'PreToolUse' and tool in ('Bash', 'exec_command') and state.get('prompt'):
                command = event.get('tool_input', {}).get('command', event.get('tool_input', {}).get('cmd', ''))
                call = event.get('tool_use_id')
                if call and not LISTENER.search(command):
                    state.setdefault('tasks', {})[call] = {'at': time.time()}
        elif kind == 'PostToolUse':
            task = state.get('tasks', {}).get(event.get('tool_use_id'))
            running = running_session(event.get('tool_response'))
            if task and running:
                task['process_session'] = running
            else:
                state.get('tasks', {}).pop(event.get('tool_use_id'), None)
            if event.get('tool_name', '').split('.')[-1] == 'write_stdin' and not running:
                process = str(event.get('tool_input', {}).get('session_id', ''))
                for call, task in list(state.get('tasks', {}).items()):
                    if task.get('process_session') == process:
                        state['tasks'].pop(call, None)
            fingerprint = tool_fingerprint(event)
            for key, question in list(state.get('questions', {}).items()):
                matches = (question['call_id'] == event.get('tool_use_id') if question.get('call_id')
                           else question['fingerprint'] == fingerprint)
                if matches and (not question['async'] or tool_failed(event.get('tool_response'))):
                    state['questions'].pop(key, None)
            for key, request in list(state.get('input_pending', {}).items()):
                matches = (request['call_id'] == event.get('tool_use_id') if request.get('call_id')
                           else request['fingerprint'] == fingerprint)
                if matches:
                    state['input_pending'].pop(key, None)
        elif kind == 'Stop':
            state['input_pending'] = {}
            if not state.get('questions') and state.get('prompt') and state.get('notified') != state['prompt']:
                token = uuid.uuid4().hex
                path = event.get('transcript_path')
                state['pending'] = {'token': token, 'at': time.time(), 'transcript': path,
                                    'offset': transcript_size(path)}
    if token:
        launch_worker(session, token)
    if input_job:
        launch_input_worker(*input_job)
    if delivery_key:
        launch_delivery_worker(session, delivery_key)
    if owner_job:
        launch_owner_watcher(*owner_job)


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
        elif sys.argv[1:2] == ['--deliver']:
            if config:
                delivery_worker(sys.argv[2], sys.argv[3], config)
        elif sys.argv[1:2] == ['--watch']:
            if config:
                watch_owner(sys.argv[2], sys.argv[3], config)
        elif sys.argv[1:2] == ['--exit']:
            if config:
                report_exit(sys.argv[2], int(sys.argv[3]), config)
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
