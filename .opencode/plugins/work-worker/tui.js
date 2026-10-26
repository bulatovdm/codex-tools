// work-worker-managed-opencode-stub-v1 (tui)
// Stable project entry: the implementation lives in the Work Worker install and is
// resolved on every setup, so updating Work Worker never rewrites this file.
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {pathToFileURL} from 'node:url';

const run=promisify(execFile);

export default {
  id:'work-worker',
  async setup(ctx){
    let plugin;
    try {
      const {stdout}=await run(process.env.WORK_WORKER_BIN||'work-worker',['opencode-plugin','--kind','tui'],{timeout:15000,maxBuffer:64*1024});
      plugin=(await import(pathToFileURL(JSON.parse(stdout).path).href)).default;
    } catch {return async()=>{};}
    return plugin.setup(ctx);
  },
};
