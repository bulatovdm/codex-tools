import contextlib
import importlib.util
import io
import json
import os
import runpy
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).parents[1]
spec=importlib.util.spec_from_file_location('startup_launcher',ROOT/'scripts/codex-tools.py')
launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)

class Terminal(io.StringIO):
    def isatty(self):return True

class WorkWorkerStartupTests(unittest.TestCase):
    def test_connected_empty_run_without_app_server_records_the_fallback_prompt(self):
        with tempfile.TemporaryDirectory() as d:
            home=Path(d);(home/'.local/bin').mkdir(parents=True);(home/'.local/bin/codex').touch()
            captured={}
            def command(args,**kwargs):
                if 'startup-context' in args:
                    return subprocess.CompletedProcess(args,0,json.dumps({'version':1,'enabled':True,'project_root':str(home)}),'')
                return subprocess.CompletedProcess(args,0)
            def attach(args):
                run=next((home/'.local/share/codex-tools/runs').iterdir())
                captured.update(json.loads((run/'launch.json').read_text()))
                (run/'exit-code').write_text('0');return 0
            with patch.dict(os.environ,{'WORK_WORKER_HOME':'/fixture/explicit-runtime'}),patch.object(Path,'home',return_value=home),patch.object(launcher.os,'getcwd',return_value=str(home)),patch.object(launcher,'tmux_path',return_value='/fixture/tmux'),patch.object(launcher.shutil,'which',return_value='/fixture/work-worker'),patch.object(sys,'stdin',Terminal()),contextlib.redirect_stdout(Terminal()),patch.object(launcher.subprocess,'run',side_effect=command),patch.object(launcher.subprocess,'call',side_effect=attach),patch.object(launcher,'create_native_thread',return_value=None):
                self.assertEqual(launcher.run([]),0)
            self.assertTrue(captured['args'],'connected empty launch must initialize without manual input')
            self.assertEqual(captured['args'],['--',launcher.WORK_WORKER_STARTUP_PROMPT])
            self.assertNotIn('--no-daemon',captured['args'],'existing launch.py owns that native option')
            self.assertNotIn('--dangerously-bypass-hook-trust',captured['args'])
            self.assertEqual(captured['environment'].get('WORK_WORKER_HOME'),'/fixture/explicit-runtime','tmux shell startup must not replace the terminal runtime override')

    def invoke(self,args,cwd,response=None,error=None,executable='/fixture/work-worker'):
        if response is None:response={'version':1,'enabled':True,'project_root':str(cwd)}
        result=subprocess.CompletedProcess([],0,json.dumps(response),'')
        with patch.object(launcher.shutil,'which',return_value=executable),patch.object(launcher.subprocess,'run',return_value=result,side_effect=error) as query,patch.object(launcher,'create_native_thread',return_value=None):
            actual,native=launcher.startup_arguments(args,str(cwd))
        self.assertIsNone(native,'only a prepared thread yields an identity')
        return actual,query

    def test_selected_directory_and_model_are_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve();sub=root/'src';sub.mkdir()
            args=['-C',str(sub),'-m','fixture-model']
            actual,query=self.invoke(args,root,{'version':1,'enabled':True,'project_root':str(root)})
            self.assertEqual(actual,args+['--',launcher.WORK_WORKER_STARTUP_PROMPT])
            self.assertEqual(query.call_args.kwargs['cwd'],str(sub))
            self.assertLessEqual(query.call_args.kwargs['timeout'],2)
            self.assertEqual(query.call_args.args[0],['/fixture/work-worker','startup-context'])

    def test_tasks_resume_other_modes_and_unknown_options_never_query_or_change_args(self):
        for args in [['fix this'],['resume'],['resume','native-id'],['fork'],['exec','task'],['--help'],['-V'],['--worktree'],['-i','image.png'],['--remote','unix:///socket'],['-c','mcp_servers.work_worker.enabled=false'],['--future-option'],['-m']]:
            with self.subTest(args=args):
                actual,query=self.invoke(args,Path.cwd())
                self.assertEqual(actual,args);query.assert_not_called()

    def test_missing_disabled_malformed_or_failed_context_preserves_empty_native_launch(self):
        cwd=Path.cwd()
        for response in [{'version':1,'enabled':False},{'version':2,'enabled':True,'project_root':str(cwd)},{'version':1,'enabled':True,'project_root':'/unrelated-root'},{}]:
            with self.subTest(response=response):self.assertEqual(self.invoke([],cwd,response)[0],[])
        self.assertEqual(self.invoke([],cwd,error=subprocess.TimeoutExpired('fixture',1))[0],[])
        self.assertEqual(self.invoke([],cwd,error=OSError('missing'))[0],[])
        actual,query=self.invoke([],cwd,executable=None);self.assertEqual(actual,[]);query.assert_not_called()

    def test_native_launcher_restores_terminal_runtime_and_its_deliberate_absence(self):
        for forwarded,expected in [({},None),({'WORK_WORKER_HOME':'/from-terminal'},'/from-terminal')]:
            with self.subTest(forwarded=forwarded),tempfile.TemporaryDirectory() as d:
                home=Path(d);run=home/'run';run.mkdir();(run/'ready').touch()
                (run/'launch.json').write_text(json.dumps({'cwd':str(home),'cli':'/fixture/codex','args':[],'environment':forwarded}))
                observed={}
                def native(args,**kwargs):observed['home']=os.environ.get('WORK_WORKER_HOME');return 0
                with patch.object(Path,'home',return_value=home),patch.object(sys,'argv',['launch.py',str(run)]),patch.dict(os.environ,{'WORK_WORKER_HOME':'/from-shell'}),patch('subprocess.call',side_effect=native):
                    with self.assertRaises(SystemExit) as exited:runpy.run_path(str(ROOT/'scripts/launch.py'),run_name='__main__')
                self.assertEqual(exited.exception.code,0)
                self.assertEqual(observed['home'],expected)

    def test_directory_resolution_failure_keeps_the_optional_feature_from_blocking_codex(self):
        with patch.object(launcher.pathlib.Path,'resolve',side_effect=OSError('unavailable directory')):
            self.assertEqual(launcher.startup_arguments([],os.getcwd()),([],None))

    def test_explicit_client_error_and_non_json_query_do_not_block_codex(self):
        for result in [subprocess.CompletedProcess([],2,'','unsupported command'),subprocess.CompletedProcess([],0,'not json','')]:
            with patch.object(launcher.shutil,'which',return_value='/fixture/work-worker'),patch.object(launcher.subprocess,'run',return_value=result):
                self.assertEqual(launcher.startup_arguments([],os.getcwd()),([],None))

if __name__=='__main__':unittest.main()
