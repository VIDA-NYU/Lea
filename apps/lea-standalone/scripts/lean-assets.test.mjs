import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import {leanAssets} from './lean-assets.mjs';

test('production and dev serve the same fixed InfoView assets without accepting extra paths', () => {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'lea-assets-'));
  try {
    const dist=path.join(root,'view'); const monaco=path.join(root,'monaco');
    fs.mkdirSync(dist); fs.mkdirSync(path.join(monaco,'webview'),{recursive:true});
    fs.writeFileSync(path.join(dist,'index.js'),'view');
    fs.writeFileSync(path.join(dist,'codicon.ttf'),'font');
    fs.writeFileSync(path.join(monaco,'webview/webview.js'),'webview');
    const plugin=leanAssets({infoviewDist:dist,lean4monacoDist:monaco});
    plugin.configResolved({root,build:{outDir:'output'}});
    plugin.closeBundle();
    assert.equal(fs.readFileSync(path.join(root,'output/infoview/index.js'),'utf8'),'view');
    assert.equal(fs.readFileSync(path.join(root,'output/infoview/webview.js'),'utf8'),'webview');
    assert.equal(fs.readFileSync(path.join(root,'output/assets/codicon.ttf'),'utf8'),'font');
    let middleware;
    plugin.configureServer({middlewares:{use:fn=>{middleware=fn;}}});
    const req={url:'/infoview/index.js?v=1'};
    middleware(req,{},()=>{});
    assert.equal(req.url,`/@fs/${dist}/index.js`);
    for (const url of ['/infoview/../../secret','/infoview/unknown.js']) {
      const unknown={url}; middleware(unknown,{},()=>{}); assert.equal(unknown.url,url);
    }
  } finally {fs.rmSync(root,{recursive:true,force:true});}
});

test('Vite development middleware serves the same editor URLs', async () => {
  const {createServer}=await import('vite');
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'lea-assets-http-'));
  let server;
  try {
    const dist=path.join(root,'view'); const monaco=path.join(root,'monaco');
    fs.mkdirSync(dist);fs.mkdirSync(path.join(monaco,'webview'),{recursive:true});
    fs.writeFileSync(path.join(dist,'index.js'),'export const view = true;');
    fs.writeFileSync(path.join(dist,'codicon.ttf'),'font');
    fs.writeFileSync(path.join(monaco,'webview/webview.js'),'export const webview = true;');
    server=await createServer({root,configFile:false,plugins:[leanAssets({infoviewDist:dist,lean4monacoDist:monaco})],server:{host:'127.0.0.1',port:0}});
    await server.listen();
    const base=`http://127.0.0.1:${server.httpServer.address().port}`;
    const response=await fetch(`${base}/infoview/webview.js`);
    assert.equal(response.status,200);
    assert.match(await response.text(),/export const webview = true/);
    const font=await fetch(`${base}/assets/codicon.ttf`);
    assert.equal(await font.text(),'font');
  } finally {await server?.close();fs.rmSync(root,{recursive:true,force:true});}
});
