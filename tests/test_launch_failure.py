import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
class Terminal(io.StringIO):
    def isatty(self): return True

class LaunchFailureTests(unittest.TestCase):
    def test_failed_cli_retains_run_and_prints_private_diagnostic_location(self):
        spec=importlib.util.spec_from_file_location('launcher',ROOT/'scripts/codex-tools.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as d:
            home=Path(d);(home/'.local/bin').mkdir(parents=True);(home/'.local/bin/codex').touch()
            def attach(args):
                run=next((home/'.local/share/codex-tools/runs').iterdir())
                (run/'exit-code').write_text('7')
                return 0
            output=Terminal()
            with patch.object(Path,'home',return_value=home), patch.object(module,'tmux_path',return_value='/fixture/tmux'), patch.object(sys,'stdin',Terminal()), contextlib.redirect_stdout(output), patch.object(module.subprocess,'run'), patch.object(module.subprocess,'call',side_effect=attach):
                self.assertEqual(module.run(['resume','native-fixture']),7)
            runs=list((home/'.local/share/codex-tools/runs').iterdir())
            self.assertEqual(len(runs),1,'failure evidence must survive')
            self.assertEqual((runs[0]/'exit-code').read_text(),'7')
            self.assertIn(str(runs[0]),output.getvalue())

    def test_launch_captures_only_own_pane_on_failure_in_private_file(self):
        with tempfile.TemporaryDirectory() as d:
            home=Path(d);run=home/'run';run.mkdir();(run/'ready').touch()
            tmux=home/'.local/share/codex-tools/tmux/bin/tmux';tmux.parent.mkdir(parents=True);tmux.touch()
            (run/'launch.json').write_text(json.dumps({'cwd':str(home),'cli':'/fixture/codex','args':['resume','native-fixture'],'environment':{}}))
            capture=subprocess.CompletedProcess([],0,'Error: fixture startup failure\n','')
            with patch.object(Path,'home',return_value=home),patch.object(sys,'argv',['launch.py',str(run)]),patch.dict(os.environ,{'TMUX':'/fixture/own-socket,123,0','TMUX_PANE':'%42'}),patch('subprocess.call',return_value=7),patch('subprocess.run',return_value=capture) as command:
                with self.assertRaises(SystemExit) as exited:runpy.run_path(str(ROOT/'scripts/launch.py'),run_name='__main__')
            self.assertEqual(exited.exception.code,7)
            log=run/'terminal-error.log';self.assertEqual(log.read_text(),'Error: fixture startup failure\n');self.assertEqual(log.stat().st_mode&0o777,0o600)
            args=next(call.args[0] for call in command.call_args_list if 'capture-pane' in call.args[0]);self.assertEqual(args[-2:],['-t','%42'])
    def simulate_failure(self, text, code=1):
        spec=importlib.util.spec_from_file_location('friendly_launcher',ROOT/'scripts/codex-tools.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as d:
            home=Path(d);(home/'.local/bin').mkdir(parents=True);(home/'.local/bin/codex').touch()
            def attach(args):
                run=next((home/'.local/share/codex-tools/runs').iterdir())
                (run/'exit-code').write_text(str(code));(run/'terminal-error.log').write_text(text)
                return 0
            output=Terminal()
            with patch.object(Path,'home',return_value=home),patch.object(module,'tmux_path',return_value='/fixture/tmux'),patch.object(sys,'stdin',Terminal()),contextlib.redirect_stdout(output),patch.object(module.subprocess,'run'),patch.object(module.subprocess,'call',side_effect=attach):
                self.assertEqual(module.run(['resume','fixture']),code)
            return output.getvalue()

    def test_failed_resume_explains_incomplete_uuid_and_offers_picker(self):
        bad_id='01a13bc1-3679-7623-b654-46217682d3e'
        output=self.simulate_failure('ERROR: No saved session found with ID '+bad_id+'. Run `codex resume` without an ID to choose from existing sessions.')
        self.assertIn('не найдена',output)
        self.assertIn(bad_id,output)
        self.assertIn('35',output);self.assertIn('36',output)
        self.assertIn('cx resume',output)

    def test_other_cli_error_is_shown_without_terminal_escapes_or_conversation(self):
        output=self.simulate_failure('private conversation must not print\n\x1b[31mERROR:\x1b[0m unexpected argument --fixture-bad\n')
        self.assertIn('unexpected argument --fixture-bad',output)
        self.assertNotIn('\x1b',output)
        self.assertNotIn('private conversation',output)

if __name__=='__main__':unittest.main()
