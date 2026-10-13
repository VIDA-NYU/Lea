// Audit reproduction: real request handler, fake adapter, no listening socket.
import { createServer } from '../../apps/overleaf-extension/companion/server.mjs';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { EventEmitter } from 'node:events';
const tmp = await fs.mkdtemp(path.join(os.tmpdir(),'lea-audit-host-'));
const server = await createServer({settingsPath:path.join(tmp,'settings.json'),envPath:path.join(tmp,'.env'),
  jobsPath:path.join(tmp,'jobs.json'),chatSessionsPath:path.join(tmp,'chat.json'),env:{},
  fetchImpl: async url => String(url).endsWith('/export')
    ? new Response('AUDIT_PROJECT_CONTENT', {status:200,headers:{'content-type':'application/zip'}})
    : new Response('{}', {status:200,headers:{'content-type':'application/json'}})
});
try {
  const request = new EventEmitter();
  Object.assign(request,{method:'GET',url:'/project-export?overleafProjectId=audit-document',headers:{host:'attacker.example:31245'}});
  const result = await new Promise((resolve,reject) => {
    const response = {status:200,headers:{},setHeader(k,v) {this.headers[k]=v;},
      writeHead(status,headers) {this.status=status;Object.assign(this.headers,headers);},
      end(body) {resolve({status:this.status,body:body?.toString()});}};
    server.emit('request',request,response);
    setTimeout(()=>reject(new Error('no response')),5000).unref();
  });
  console.log('non_loopback_host_without_origin:',result);
} finally {
  server.emit('close');
  await fs.rm(tmp,{recursive:true,force:true});
}
