// Publisher and installer run the same real smoke checks before activation.
import {spawn, spawnSync} from 'node:child_process';
import {readFileSync, writeFileSync, mkdtempSync, rmSync} from 'node:fs';
import {join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {createInterface} from 'node:readline';
import {createServer} from 'node:http';

const root = fileURLToPath(new URL('.', import.meta.url));
const meta = JSON.parse(readFileSync(join(root, 'runtime.json'), 'utf8'));
const catalog = JSON.parse(readFileSync(join(root, 'catalog.json'), 'utf8'));
if (meta.components.length !== 18 || catalog.components.some(c => !meta.components.some(m => m.id === c.id))) throw Error('Missing component');
for (const c of meta.components) readFileSync(join(root, c.evidence));
const run = (name,args) => {const r=spawnSync(join(root,meta.executables[name]),args,{encoding:'utf8',windowsHide:true});if(r.status!==0)throw Error(r.stderr||name);return r.stdout;};
run('ffmpeg',['-version']);run('ffprobe',['-version']);
process.env.PLAYWRIGHT_BROWSERS_PATH = join(root,'browsers');
const {chromium} = await import('playwright');
const server=createServer((req,res)=>{
  if(req.url==='/smoke.js'){res.setHeader('Content-Type','text/javascript');res.end(readFileSync(join(root,'smoke.js')));}
  else res.end('<!doctype html><html><body><script src="/smoke.js"></script></body></html>');
});
await new Promise(r=>server.listen(0,'127.0.0.1',r));
let browser, child;
const tmp=mkdtempSync(join(root,'smoke-'));
try {
  browser=await chromium.launch({headless:true,args:['--no-sandbox']});
  const page=await browser.newPage();
  await page.goto('http://127.0.0.1:'+server.address().port);
  await page.waitForFunction(()=>window.__MINMIN_COMPONENTS_READY===true,{timeout:30000});
  const count=await page.evaluate(()=>window.__MINMIN_COMPONENT_COUNT);
  if(count!==15)throw Error('Browser components not ready: '+count);
  await browser.close();browser=null;
  child=spawn(join(root,meta.executables.concat),['--mcp','headless'],{
    cwd:root,env:{...process.env,CONCAT_MCP_ROOTS:tmp},stdio:['pipe','pipe','pipe'],windowsHide:true});
  let seq=0;const pending=new Map();let errors='';
  child.stderr.on('data',b=>{errors=(errors+b).slice(-4000);});
  child.on('error',e=>{for(const p of pending.values())p.reject(e);});
  child.on('exit',()=>{for(const p of pending.values())p.reject(Error('Concat exited: '+errors));});
  createInterface({input:child.stdout}).on('line',line=>{const m=JSON.parse(line),p=pending.get(m.id);if(!p)return;pending.delete(m.id);if(m.error||m.result?.isError)p.reject(Error(JSON.stringify(m)));else p.resolve(m.result);});
  const rpc=(method,params)=>new Promise((resolve,reject)=>{const id=++seq;const timer=setTimeout(()=>{pending.delete(id);reject(Error('MCP timeout '+method));},120000);pending.set(id,{resolve:v=>{clearTimeout(timer);resolve(v);},reject:e=>{clearTimeout(timer);reject(e);}});child.stdin.write(JSON.stringify({jsonrpc:'2.0',id,method,params})+'\n');});
  await rpc('initialize',{protocolVersion:'2025-06-18',capabilities:{},clientInfo:{name:'minmin-smoke',version:'1'}});
  child.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n');
  const call=async(name,args)=>{const r=await rpc('tools/call',{name,arguments:args});return r.structuredContent||r;};
  await call('concat_version',{});
  await call('project_create',{location:tmp,name:'test',video:{width:160,height:90,rateNum:10,rateDen:1}});
  const path=join(tmp,'test');
  await call('title_add',{path,start:0,duration:0.3,above:true,style:{content:'MinMin'}});
  const job=await call('export_start',{path,output:join(tmp,'test.mp4'),preset:'ultrafast'});
  const end=await call('export_wait',{job:job.job,timeoutSeconds:120});
  if(end.state!=='done')throw Error('Export failed '+JSON.stringify(end));
  const probe=JSON.parse(run('ffprobe',['-v','error','-show_streams','-of','json',join(tmp,'test.mp4')]));
  if(!probe.streams.some(s=>s.codec_type==='video'&&s.width===160))throw Error('Invalid video');
  await call('project_close',{path,save:true});
  const scene=join(tmp,'scene.mjs');
  writeFileSync(scene,"import {gsap} from 'gsap'; import {Color} from 'three'; export function drawFrame(ctx,t,{width,height}) {ctx.fillStyle='#ff7a3c';ctx.fillRect(0,0,width,height);ctx.fillStyle=new Color('black').getStyle();ctx.font='20px Arial';ctx.fillText('MinMin '+gsap.parseEase('power2.out')(t).toFixed(2),10,40);}");
  run('node',[join(root,'render-scene.mjs'),'--entry',scene,'--assets',tmp,'--output',join(tmp,'scene.mp4'),'--duration','0.2','--width','160','--height','90']);
  run('ffprobe',['-v','error','-show_streams',join(tmp,'scene.mp4')]);
  console.log('PASS: 18 components present, 15 browser modules loaded, Concat title exported to MP4, FFprobe verified');
} finally {
  if(browser)await browser.close();
  if(child){child.stdin.end();child.kill();await new Promise(r=>child.exitCode!==null?r():child.once('exit',r));}
  server.close();
  rmSync(tmp,{recursive:true,force:true});
}
