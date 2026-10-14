#!/usr/bin/python3
import argparse
import datetime
import fcntl
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile


def timestamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def atomic_write(path, data, mode=0o600, expected=None):
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=str(path.parent))
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        if expected is not None and path.read_bytes() != expected:
            raise RuntimeError('Configuration changed concurrently: ' + str(path))
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def probe_cli(cli, state):
    if not cli.is_file() or not os.access(cli, os.X_OK):
        raise RuntimeError('Codex CLI is missing or not executable: ' + str(cli))
    real = cli.resolve()
    details = real.stat()
    identity = [str(real), details.st_ino, details.st_size, details.st_mtime_ns, os.uname().release]
    cached = state / 'sandbox-probe.json'
    if cached.exists():
        try:
            if json.loads(cached.read_text()).get('identity') == identity:
                return
        except (ValueError, OSError):
            pass
    result = subprocess.run([str(cli), 'sandbox', '/usr/bin/true'], cwd=str(cli.parent), env={**os.environ, 'LC_ALL': 'en_US.UTF-8'}, capture_output=True, text=True, timeout=20)
    if result.returncode != 0:
        raise RuntimeError('Codex sandbox probe failed: ' + (result.stderr or result.stdout).strip()[-1000:])
    atomic_write(cached, json.dumps({'identity': identity, 'at': timestamp()}).encode())


def readable_configuration(path, root):
    if path.is_symlink():
        raise RuntimeError('Refusing configuration symlink: ' + str(path))
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        raise RuntimeError('Refusing configuration symlink outside Codex: ' + str(path))
    return path.read_bytes()


def node_repl_plan(config, cli, root):
    if not config.exists():
        return None
    before = readable_configuration(config, root)
    lines = before.decode().splitlines(keepends=True)
    selected = False
    found = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith('['):
            selected = stripped.split('#', 1)[0].strip() in ['[mcp_servers.node_repl.env]', '[mcp_servers."node_repl".env]']
            continue
        if not selected:
            continue
        match = re.match(r'^(\s*CODEX_CLI_PATH\s*=\s*)("(?:[^"\\]|\\.)*"|\x27[^\x27]*\x27)(\s*(?:#.*)?)(\r?\n)?$', line)
        if match is None:
            if stripped.startswith('CODEX_CLI_PATH'):
                raise ValueError('Unsupported CODEX_CLI_PATH syntax: ' + str(config))
            continue
        if found:
            raise ValueError('Duplicate CODEX_CLI_PATH: ' + str(config))
        found = True
        current = json.loads(match.group(2)) if match.group(2).startswith('"') else match.group(2)[1:-1]
        if current != str(cli):
            lines[index] = match.group(1) + json.dumps(str(cli)) + match.group(3) + (match.group(4) or '')
    after = ''.join(lines).encode()
    return (config, before, after) if after != before else None


def plugin_plan(path, cli, root):
    before = readable_configuration(path, root)
    parsed = json.loads(before)
    if not isinstance(parsed, dict) or not isinstance(parsed.get('mcpServers'), dict):
        raise ValueError('Unsupported MCP configuration: ' + str(path))
    server = parsed['mcpServers'].get('cua_repl')
    if server is None:
        return None
    if not isinstance(server, dict) or not isinstance(server.get('env'), dict):
        raise ValueError('Unsupported cua_repl environment: ' + str(path))
    environment = server['env']
    if 'CODEX_CLI_PATH' not in environment:
        raise ValueError('Missing cua_repl CODEX_CLI_PATH: ' + str(path))
    if environment['CODEX_CLI_PATH'] == str(cli):
        return None
    environment['CODEX_CLI_PATH'] = str(cli)
    return path, before, (json.dumps(parsed, ensure_ascii=False, indent=2) + '\n').encode()


def repair(user, cli, state):
    state.mkdir(parents=True, exist_ok=True)
    state.chmod(0o700)
    with (state / 'guard.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        probe_cli(cli, state)
        root = user / '.codex'
        plans = []
        config = node_repl_plan(root / 'config.toml', cli, root)
        if config is not None:
            plans.append(config)
        plugins = root / 'plugins/cache/openai-bundled/unified-computer-use'
        for name in ['.mcp.json', 'mcp.json']:
            for path in sorted(plugins.glob('*/' + name)):
                plan = plugin_plan(path, cli, root)
                if plan is not None:
                    plans.append(plan)
        backups = state / 'backups'
        if plans:
            backups.mkdir(exist_ok=True)
            backups.chmod(0o700)
        changed = []
        for path, before, after in plans:
            identity = hashlib.sha256(str(path).encode() + b'\0' + before).hexdigest()
            backup = backups / (identity + '.backup')
            if not backup.exists():
                atomic_write(backup, before)
            mode = path.stat().st_mode & 0o777
            atomic_write(path, after, mode, expected=before)
            changed.append(str(path))
        atomic_write(state / 'status.json', json.dumps({'at': timestamp(), 'healthy': True, 'cli': str(cli), 'changed': changed, 'plugin_versions': sorted(path.name for path in plugins.iterdir() if path.is_dir()) if plugins.exists() else []}).encode())
        if changed:
            record(state, {'at': timestamp(), 'kind': 'repaired', 'files': changed})
        return changed


def record(state, value):
    with (state / 'events.jsonl').open('a') as output:
        output.write(json.dumps(value) + '\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--quiet', action='store_true')
    arguments = parser.parse_args()
    user = pathlib.Path.home()
    state = user / '.local/share/codex-browser-guard/state'
    cli = user / '.local/bin/codex'
    try:
        changed = repair(user, cli, state)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        state.mkdir(parents=True, exist_ok=True)
        status = state / 'status.json'
        try:
            previous = json.loads(status.read_text()) if status.exists() else {}
        except (ValueError, OSError):
            previous = {}
        reason = str(error)
        if previous.get('error') != reason:
            record(state, {'at': timestamp(), 'kind': 'refused', 'reason': reason})
        atomic_write(status, json.dumps({'at': timestamp(), 'healthy': False, 'error': reason}).encode())
        print('Codex browser guard: ' + reason, file=sys.stderr)
        return 1
    if not arguments.quiet:
        print('Codex browser guard: sandbox OK; repaired ' + str(len(changed)) + ' configurations')
    return 0


if __name__ == '__main__':
    sys.exit(main())
