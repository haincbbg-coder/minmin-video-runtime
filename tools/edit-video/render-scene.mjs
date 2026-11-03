// Deterministic browser scene renderer, independently implemented for MinMin.
// Project JS runs in Chromium, never as a Node module in the MinMin server.
import {readFileSync,mkdtempSync,rmSync,existsSync,realpathSync} from 'node:fs';
import {resolve,join,dirname,sep,extname} from 'node:path';
import {fileURLToPath} from 'node:url';
import {createServer} from 'node:http';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {build} from 'esbuild';

const root=fileURLToPath(new URL('.',import.meta.url));
process.env.PLAYWRIGHT_BROWSERS_PATH=join(root,'browsers');
const {chromium}=await import('playwright');
const argv=process.argv.slice(2),get=k=>argv.includes(k)?argv[argv.indexOf(k)+1]:undefined;
for(const k of ['--entry','--output','--assets','--duration'])if(!argv.includes(k))throw Error('Missing '+k);
const entry=resolve(get('--entry')),output=resolve(get('--output')),assets=resolve(get('--assets'));
const duration=Number(get('--duration')),width=Number(get('--width')||1280),height=Number(get('--height')||720),fps=Number(get('--fps')||30);
if(!Number.isFinite(duration)||duration<=0||duration>120||![width,height,fps].every(Number.isInteger)||width<16||height<16||width>1920||height>1920||fps<1||fps>60)throw Error('Invalid render settings');
if(existsSync(output))throw Error('Output already exists');
if(!entry.startsWith(assets+sep))throw Error('Entry must be within assets root');
const tmp=mkdtempSync(join(dirname(output),'scene-'));
let browser,encoder;
const bundled=await build({entryPoints:[entry],bundle:true,platform:'browser',format:'esm',write:false,
  nodePaths:[join(root,'node_modules')],define:{'process.env.NODE_ENV':'"production"'},
  plugins:[{name:'minmin-scope',setup(b){b.onLoad({filter:/.*/},a=>{
    const p=realpathSync(a.path),r=realpathSync(root),v=realpathSync(assets);
    if(!p.startsWith(r+sep)&&!p.startsWith(v+sep))throw Error('Import outside project/runtime');
    return null;
  });}}]});
const module=bundled.outputFiles[0].contents;
const allowed=(base,value)=>{const p=realpathSync(resolve(base,value)),r=realpathSync(base);if(p!==r&&!p.startsWith(r+sep))throw Error('Invalid path');return p;};
const html=`<!doctype html><html><body style="margin:0"><canvas id="scene" width="${width}" height="${height}"></canvas><script type="module">import * as scene from '/scene.js';const canvas=document.querySelector('canvas');window.draw=async t=>{await scene.drawFrame(canvas.getContext('2d'),t,{width:${width},height:${height},canvas});};window.ready=true;</script></body></html>`;
const server=createServer((req,res)=>{
 try{
  const url=new URL(req.url,'http://localhost');
  if(url.pathname==='/'){res.setHeader('Content-Type','text/html');res.end(html);return;}
  if(url.pathname==='/scene.js'){res.setHeader('Content-Type','text/javascript');res.end(module);return;}
  let p;
  if(url.pathname.startsWith('/assets/'))p=allowed(assets,decodeURIComponent(url.pathname.slice(8)));
  else if(url.pathname.startsWith('/runtime/'))p=allowed(resolve(root),decodeURIComponent(url.pathname.slice(9)));
  else{res.writeHead(404);res.end();return;}
  const mime={'.wasm':'application/wasm','.png':'image/png','.jpg':'image/jpeg','.json':'application/json','.js':'text/javascript','.ttf':'font/ttf','.woff2':'font/woff2'};
  res.setHeader('Content-Type',mime[extname(p)]||'application/octet-stream');res.end(readFileSync(p));
 }catch{res.writeHead(404);res.end();}
});
try{
 await new Promise(r=>server.listen(0,'127.0.0.1',r));
 const origin='http://127.0.0.1:'+server.address().port;
 browser=await chromium.launch({headless:true,args:process.platform==='linux'&&process.getuid?.()===0?['--no-sandbox']:[]});
 const page=await browser.newPage({viewport:{width,height}});
 await page.route('**/*',route=>route.request().url().startsWith(origin+'/')||route.request().url().startsWith('data:')?route.continue():route.abort());
 await page.goto(origin);await page.waitForFunction(()=>window.ready===true);
 const meta=JSON.parse(readFileSync(join(root,'runtime.json'),'utf8'));
 encoder=spawn(join(root,meta.executables.ffmpeg),['-v','error','-n','-f','image2pipe','-framerate',String(fps),'-i','pipe:0','-an','-c:v','libx264','-pix_fmt','yuv420p','-movflags','+faststart',output],{windowsHide:true,stdio:['pipe','ignore','pipe']});
 let errors='';encoder.stderr.on('data',b=>{errors=(errors+b).slice(-4000);});
 const ended=new Promise((resolve,reject)=>{encoder.on('error',reject);encoder.on('exit',code=>code===0?resolve():reject(Error(errors||'FFmpeg failed')));});
 // Attach a rejection handler immediately while frames are still being written.
 ended.catch(()=>{});
 for(let frame=0;frame<Math.ceil(duration*fps);frame++){
  await page.evaluate(async t=>{await window.draw(t);},frame/fps);
  const bytes=await page.locator('#scene').screenshot({type:'png'});
  if(!encoder.stdin.write(bytes))await once(encoder.stdin,'drain');
 }
 encoder.stdin.end();await ended;
 console.log(JSON.stringify({output,width,height,fps,duration}));
}finally{
 if(browser)await browser.close();if(encoder&&encoder.exitCode===null)encoder.kill();
 server.close();rmSync(tmp,{recursive:true,force:true});
}
