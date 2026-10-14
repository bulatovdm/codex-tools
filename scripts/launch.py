#!/usr/bin/python3
import json
import os
import pathlib
import subprocess
import sys
import time

directory = pathlib.Path(sys.argv[1])
launch = json.loads((directory / 'launch.json').read_text())
deadline = time.monotonic() + 10
while not (directory / 'ready').exists():
    if time.monotonic() > deadline:
        sys.exit('codex-tools: terminal setup did not finish')
    time.sleep(0.05)
os.environ['CODEX_TOOLS_RUN'] = directory.name
os.environ.update(launch['environment'])
guard = pathlib.Path.home() / '.local/bin/codex-browser-guard'
if guard.exists():
    subprocess.run([str(guard), '--quiet'])
try:
    result = subprocess.call([launch['cli'], '--no-daemon', *launch['args']], cwd=launch['cwd'])
except OSError as error:
    print(error, file=sys.stderr)
    result = 1
(directory / 'exit-code').write_text(str(result))
sys.exit(result)
