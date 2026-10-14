#!/usr/bin/python3
import datetime
import json
import os
import pathlib
import plistlib
import shutil
import subprocess

def install_files(home, source):
    target = home / '.local/share/codex-tools'
    guard = home / '.local/share/codex-browser-guard'
    backups = target / 'backups' / datetime.datetime.now().strftime('%Y%m%dT%H%M%S%f')
    for directory in [target, guard, backups, home / '.local/bin', home / '.codex', home / '.config/codex-tools', home / 'Library/LaunchAgents']:
        directory.mkdir(parents=True, exist_ok=True)
    target.chmod(0o700)
    backups.chmod(0o700)

    def write(path, data, mode=0o600):
        if path.is_symlink():
            raise RuntimeError('Refusing to overwrite a symlink: ' + str(path))
        if path.exists() and path.read_bytes() == data:
            return
        if path.exists():
            backup = backups / str(path.relative_to(home)).replace('/', '__')
            backup.write_bytes(path.read_bytes())
            backup.chmod(0o600)
        path.write_bytes(data)
        path.chmod(mode)

    scripts = target / 'scripts'
    scripts.mkdir(exist_ok=True)
    for path in (source / 'scripts').glob('*.py'):
        write(scripts / path.name, path.read_bytes(), 0o700)
    write(guard / 'codex-browser-guard.py', (source / 'scripts/codex-browser-guard.py').read_bytes(), 0o700)
    for name, destination in [('codex-tools', scripts / 'codex-tools.py'), ('codex-browser-guard', guard / 'codex-browser-guard.py')]:
        command = home / '.local/bin' / name
        if command.exists() or command.is_symlink():
            if not command.is_symlink() or command.resolve() != destination.resolve():
                raise RuntimeError('Existing command belongs to another installation: ' + str(command))
        else:
            command.symlink_to(destination)
    shell = b'''codex() {
  "$HOME/.local/bin/codex-browser-guard" --quiet || print -u2 'Codex: browser guard failed; see ~/.local/share/codex-browser-guard/state/status.json'
  command "$HOME/.local/bin/codex" "$@"
}
alias cx='"$HOME/.local/bin/codex-tools" run'
'''
    write(guard / 'shell.zsh', shell)
    block = '\n# Codex browser configuration guard\n[[ ! -r "$HOME/.local/share/codex-browser-guard/shell.zsh" ]] || source "$HOME/.local/share/codex-browser-guard/shell.zsh"\n'
    for name in ['.zprofile', '.zshrc']:
        path = home / name
        content = path.read_text() if path.exists() else ''
        if 'source "$HOME/.local/share/codex-browser-guard/shell.zsh"' not in content:
            write(path, (content + block).encode())
    hooks_path = home / '.codex/hooks.json'
    hooks = json.loads(hooks_path.read_text()) if hooks_path.exists() else {'description': 'codex-tools session identity', 'hooks': {}}
    command = '/usr/bin/python3 "$HOME/.local/share/codex-tools/scripts/session-hook.py"'
    for event in ['SessionStart', 'UserPromptSubmit']:
        groups = hooks.setdefault('hooks', {}).setdefault(event, [])
        if not any(handler.get('command') == command for group in groups for handler in group.get('hooks', [])):
            groups.append({'hooks': [{'type': 'command', 'command': command, 'timeout': 3}]})
    write(hooks_path, (json.dumps(hooks, ensure_ascii=False, indent=2) + '\n').encode())
    options = home / '.config/codex-tools/status.json'
    if not options.exists():
        write(options, (source / 'examples/status.json').read_bytes())
    agent = {
        'Label': 'com.dima.codex-browser-guard',
        'ProgramArguments': ['/usr/bin/python3', str(guard / 'codex-browser-guard.py'), '--quiet'],
        'RunAtLoad': True, 'StartInterval': 30, 'ThrottleInterval': 5,
        'WatchPaths': [str(home / '.codex/config.toml'), str(home / '.codex/plugins/cache/openai-bundled'), str(home / '.codex/packages/standalone')],
        'StandardOutPath': str(guard / 'launchagent.stdout.log'),
        'StandardErrorPath': str(guard / 'launchagent.stderr.log'),
    }
    write(home / 'Library/LaunchAgents/com.dima.codex-browser-guard.plist', plistlib.dumps(agent))

def main():
    source = pathlib.Path(__file__).resolve().parents[1]
    subprocess.run(['/usr/bin/python3', '-m', 'unittest', 'discover', '-s', str(source / 'tests')], check=True)
    home = pathlib.Path.home()
    install_files(home, source)
    subprocess.run([str(home / '.local/bin/codex-browser-guard')], check=True)
    subprocess.run(['/bin/zsh', '-n', str(home / '.local/share/codex-browser-guard/shell.zsh')], check=True)
    label = 'gui/' + str(os.getuid()) + '/com.dima.codex-browser-guard'
    if subprocess.run(['/bin/launchctl', 'print', label], capture_output=True).returncode == 0:
        subprocess.run(['/bin/launchctl', 'bootout', label], check=True)
    subprocess.run(['/bin/launchctl', 'bootstrap', 'gui/' + str(os.getuid()), str(home / 'Library/LaunchAgents/com.dima.codex-browser-guard.plist')], check=True)
    print('Installed. New terminal: cx. Review codex-tools hooks in /hooks.')

if __name__ == '__main__':
    main()
