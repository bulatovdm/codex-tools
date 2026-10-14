#!/usr/bin/python3
import json
import os
import pathlib
import re
import sys
import tempfile

def capture(value, run, root):
    if not re.fullmatch(r'[a-f0-9]{32}', run) or not isinstance(value.get('session_id'), str):
        return
    directory = root / run
    if not directory.is_dir():
        return
    descriptor, temporary = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(descriptor, 'w') as output:
            json.dump({'session_id': value['session_id'], 'cwd': value.get('cwd')}, output)
        os.replace(temporary, directory / 'session.json')
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

if __name__ == '__main__':
    try:
        capture(json.load(sys.stdin), os.environ.get('CODEX_TOOLS_RUN', ''), pathlib.Path.home() / '.local/share/codex-tools/runs')
    except (OSError, ValueError, TypeError):
        pass
