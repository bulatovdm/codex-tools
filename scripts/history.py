#!/usr/bin/env python3
"""Explicitly keep Codex input history without a size cap (Python 3.11+)."""
import argparse
import copy
import datetime
import os
import pathlib
import re
import stat
import sys
import tempfile

try:
    import tomllib
except ImportError:
    sys.exit('codex-tools history requires Python 3.11+; run with a newer python3.')


def table_path(header):
    parsed = tomllib.loads(header + '\n__codex_history_marker__ = true\n')
    keys = []
    while isinstance(parsed, dict) and len(parsed) == 1:
        key, parsed = next(iter(parsed.items()))
        if key == '__codex_history_marker__':
            return tuple(keys)
        keys.append(key)
    return None


def keep_all(text):
    before = tomllib.loads(text)
    expected = copy.deepcopy(before)
    targets = {('history',): expected.setdefault('history', {})}
    for name, profile in expected.get('profiles', {}).items():
        if 'history' in profile:
            targets[('profiles', name, 'history')] = profile['history']
    for history in targets.values():
        if not isinstance(history, dict):
            raise ValueError('history must be a TOML table')
        history['persistence'] = 'save-all'
        history.pop('max_bytes', None)
    if before == expected:
        return text

    newline = '\r\n' if '\r\n' in text else '\n'
    output = []
    selected = None
    found = set()
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith('['):
            try:
                selected = table_path(line.strip())
            except tomllib.TOMLDecodeError:
                selected = None
            if selected in targets:
                found.add(selected)
                output.append(line.rstrip('\r\n') + newline)
                output.append('persistence = "save-all"' + newline)
                continue
        if selected in targets and re.match(
                r'''\s*(?:persistence|max_bytes|"persistence"|"max_bytes"|'persistence'|'max_bytes')\s*=''', line):
            continue
        output.append(line)
    if ('history',) not in found and 'history' not in before:
        if output and not output[-1].endswith('\n'):
            output.append(newline)
        output.append(newline + '[history]' + newline + 'persistence = "save-all"' + newline)
    after = ''.join(output)
    # Full parsing detects inline/dotted keys, multiline strings that resemble
    # headers, or any other syntax the conservative editor cannot safely change.
    try:
        valid = tomllib.loads(after) == expected
    except tomllib.TOMLDecodeError:
        valid = False
    if not valid:
        raise ValueError('Unsupported history syntax; use explicit [history] / [profiles.NAME.history] tables. No files changed.')
    return after


def apply(path):
    if path.is_symlink():
        raise ValueError('Refusing configuration symlink: ' + str(path))
    before = path.read_bytes() if path.exists() else b''
    after = keep_all(before.decode('utf-8')).encode('utf-8')
    if before == after:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    backup = None
    if path.exists():
        stamp = datetime.datetime.now().strftime('%Y%m%dT%H%M%S%f')
        backup = path.with_name(path.name + '.history-backup-' + stamp)
        descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(before)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(after)
            stream.flush()
            os.fsync(stream.fileno())
        if path.is_symlink() or (path.read_bytes() if path.exists() else b'') != before:
            raise RuntimeError('Configuration changed concurrently; retry: ' + str(path))
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=pathlib.Path,
                        default=pathlib.Path(os.environ.get('CODEX_HOME', pathlib.Path.home() / '.codex')) / 'config.toml')
    parser.add_argument('--apply', action='store_true', help='save all input history and remove max_bytes; default is read-only')
    args = parser.parse_args()
    try:
        if args.config.is_symlink():
            raise ValueError('Refusing configuration symlink: ' + str(args.config))
        before = args.config.read_text() if args.config.exists() else ''
        parsed = tomllib.loads(before)
        history = parsed.get('history', {})
        print('Config: ' + str(args.config))
        print('Input history persistence: ' + str(history.get('persistence', 'save-all (default)')))
        print('Input history size cap: ' + str(history.get('max_bytes', 'unlimited (default)')))
        planned = keep_all(before)
        if args.apply:
            backup = apply(args.config)
            print('Applied: save-all; no max_bytes.' if planned != before else 'Already configured: save-all; no max_bytes.')
            if backup:
                print('Backup: ' + str(backup))
        else:
            print('Changes needed: ' + ('yes; use --apply' if planned != before else 'no'))
        print('This controls history.jsonl input history, not full session transcript retention.')
        print('No documented age-based session cleanup setting; session files are untouched.')
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print('codex-tools history: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
