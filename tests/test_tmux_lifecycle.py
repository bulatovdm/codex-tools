"""Opt-in real tmux/PTY tests, using private stand-ins, never user sessions.

CODEX_TOOLS_LIVE_TMUX=1 python3 -m unittest discover -s tests -p test_tmux_lifecycle.py
"""
import fcntl
import importlib.util
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import struct
import subprocess
import tempfile
import termios
import time
import unittest
import uuid

ROOT = Path(__file__).parents[1]
TMUX = str(Path.home() / '.local/share/codex-tools/tmux/bin/tmux')
if not Path(TMUX).exists():
    TMUX = shutil.which('tmux')


@unittest.skipUnless(os.environ.get('CODEX_TOOLS_LIVE_TMUX') == '1' and TMUX,
                     'opt-in live tmux/PTY integration')
class TmuxLifecycleTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('lifecycle_launcher', ROOT / 'scripts/codex-tools.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.tmp = tempfile.TemporaryDirectory(prefix='codex-tools-lifecycle-')
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / uuid.uuid4().hex
        self.directory.mkdir()
        self.prefix = [TMUX, '-L', 'codex-tools-' + self.directory.name, '-f', '/dev/null']
        self.clients = []
        self.addCleanup(self.cleanup_server)
        program = self.directory / 'stand-in.py'
        program.write_text('''import pathlib, signal, sys
root = pathlib.Path(sys.argv[1])
def interrupted(signum, frame):
    if (root / 'active').exists():
        (root / 'active').unlink()
        (root / 'interrupted').touch()
    else:
        (root / 'exit-code').write_text('0')
        sys.exit(0)
signal.signal(signal.SIGINT, interrupted)
(root / 'started').touch()
while True:
    line = sys.stdin.readline().strip()
    if line in ('/quit', '/exit', 'crash'):
        status = 2 if line == 'crash' else 0
        (root / 'exit-code').write_text(str(status))
        sys.exit(status)
''')
        import shlex
        command = shlex.join(['/usr/bin/python3', str(program), str(self.directory)])
        self.command('new-session', '-d', '-s', 'codex', command)
        self.wait_for(lambda: (self.directory / 'started').exists())
        self.module.configure_lifecycle(TMUX, self.directory)

    def command(self, *args, check=True):
        return subprocess.run([*self.prefix, *args], check=check, capture_output=True, text=True)

    def wait_for(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            for _, fd in self.clients:
                try:
                    if select.select([fd], [], [], 0)[0]:
                        os.read(fd, 65536)
                except OSError:
                    pass
            if predicate():
                return
            time.sleep(.025)
        self.fail('tmux lifecycle condition timed out')

    def alive(self):
        return self.command('has-session', '-t', 'codex', check=False).returncode == 0

    def attach(self):
        pid, fd = pty.fork()
        if pid == 0:
            env = dict(os.environ, TERM='xterm-256color')
            env.pop('TMUX', None)
            os.execve(TMUX, [*self.prefix, 'attach-session', '-t', 'codex'], env)
        self.clients.append((pid, fd))
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', 30, 120, 0, 0))
        self.wait_for(lambda: self.command('show-options', '-v', '-t', 'codex',
                                          'destroy-unattached', check=False).stdout.strip() == 'on')
        return fd

    def cleanup_server(self):
        self.command('kill-server', check=False)
        for pid, fd in self.clients:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                if os.waitpid(pid, os.WNOHANG)[0] == 0:
                    os.kill(pid, signal.SIGTERM)
                    os.waitpid(pid, 0)
            except (ChildProcessError, ProcessLookupError):
                pass

    def test_window_close_destroys_session(self):
        fd = self.attach()
        os.close(fd)
        self.wait_for(lambda: not self.alive())

    def test_explicit_detach_survives_and_next_window_close_ends_run(self):
        fd = self.attach()
        os.write(fd, b'\x02d')
        self.wait_for(lambda: self.command('display-message', '-p', '-t', 'codex',
                                          '#{session_attached}', check=False).stdout.strip() == '0')
        self.assertTrue(self.alive())
        self.assertEqual(self.command('show-options', '-v', '-t', 'codex',
                                      'destroy-unattached').stdout.strip(), 'off')
        fd = self.attach()
        os.close(fd)
        self.wait_for(lambda: not self.alive())

    def test_ctrl_c_interrupts_active_then_exits_idle(self):
        (self.directory / 'active').touch()
        fd = self.attach()
        os.write(fd, b'\x03')
        self.wait_for(lambda: (self.directory / 'interrupted').exists())
        self.assertTrue(self.alive())
        os.write(fd, b'\x03')
        self.wait_for(lambda: not self.alive())
        self.assertEqual((self.directory / 'exit-code').read_text(), '0')

    def test_quit_releases_session(self):
        fd = self.attach()
        os.write(fd, b'/quit\n')
        self.wait_for(lambda: not self.alive())
        self.assertEqual((self.directory / 'exit-code').read_text(), '0')

    def test_exit_releases_session(self):
        fd = self.attach()
        os.write(fd, b'/exit\n')
        self.wait_for(lambda: not self.alive())

    def test_crash_releases_session_and_keeps_status(self):
        fd = self.attach()
        os.write(fd, b'crash\n')
        self.wait_for(lambda: not self.alive())
        self.assertEqual((self.directory / 'exit-code').read_text(), '2')
