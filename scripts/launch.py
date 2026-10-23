#!/usr/bin/python3
import json
import os
import pathlib
import subprocess
import shutil
import re
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
# The tmux shell may source ~/.zshenv; restore the terminal's exact runtime choice.
os.environ.pop('WORK_WORKER_HOME', None)
os.environ.update(launch['environment'])
guard = pathlib.Path.home() / '.local/bin/codex-browser-guard'
if guard.exists():
    subprocess.run([str(guard), '--quiet'])
try:
    result = subprocess.call([launch['cli'], '--no-daemon', *launch['args']], cwd=launch['cwd'])
except OSError as error:
    print(error, file=sys.stderr)
    result = 1
if result != 0:
    pane = os.environ.get('TMUX_PANE', '')
    if os.environ.get('TMUX') and re.fullmatch(r'%[0-9]+', pane):
        installed = pathlib.Path.home() / '.local/share/codex-tools/tmux/bin/tmux'
        tmux = str(installed) if installed.exists() else shutil.which('tmux')
        if tmux:
            try:
                captured = subprocess.run([tmux, 'capture-pane', '-p', '-S', '-200', '-t', pane],
                                          capture_output=True, text=True, timeout=3)
                if captured.returncode == 0:
                    descriptor = os.open(directory / 'terminal-error.log',
                                         os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, 'w') as output:
                        output.write(captured.stdout)
            except (OSError, subprocess.TimeoutExpired):
                pass
(directory / 'exit-code').write_text(str(result))
notifier = pathlib.Path(__file__).with_name('notify-phone.py')
if notifier.exists():
    try:
        subprocess.run([sys.executable, str(notifier), '--exit', str(directory), str(result)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        pass
sys.exit(result)
