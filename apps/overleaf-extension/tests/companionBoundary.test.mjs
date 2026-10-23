import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { EventEmitter } from 'node:events';
import { createServer } from '../companion/workerService.mjs';
import { isAllowedHost } from '../companion/netguard.mjs';

test('Host parsing accepts local identities and exact configured exceptions', () => {
  for (const host of [undefined, 'localhost', 'LOCALHOST:31245', '127.0.0.1:9', '[::1]:31245', '0.0.0.0']) {
    assert.equal(isAllowedHost(host), true, String(host));
  }
  for (const host of ['localhost.evil:9', '[::1', 'localhost:abc', 'localhost:65536',
    'user@localhost', 'localhost/path', 'localhost,attacker.example', ['localhost'], ' localhost']) {
    assert.equal(isAllowedHost(host), false, String(host));
  }
  assert.equal(isAllowedHost('lab.example:9000', {LEA_ALLOWED_HOSTS: 'lab.example'}), true);
  assert.equal(isAllowedHost('lab.example:9000', {LEA_ALLOWED_HOSTS: 'lab.example:9000'}), true);
  assert.equal(isAllowedHost('lab.example:9001', {LEA_ALLOWED_HOSTS: 'lab.example:9000'}), false);
  assert.equal(isAllowedHost('evil.lab.example', {LEA_ALLOWED_HOSTS: 'lab.example'}), false);
});

test('originless attacker Host is rejected before project export or preflight dispatch', async () => {
  const tmp = await fs.mkdtemp(path.join(os.tmpdir(), 'lea-boundary-'));
  let exports = 0;
  const server = await createServer({ settingsPath: path.join(tmp, 'settings.json'),
    envPath: path.join(tmp, '.env'), jobsPath: path.join(tmp, 'jobs.json'),
    chatSessionsPath: path.join(tmp, 'chat.json'), env: {},
    fetchImpl: async url => {
      if (String(url).endsWith('/export')) exports++;
      return new Response('AUDIT_PROJECT_CONTENT', {status: 200});
    },
  });
  try {
    for (const method of ['GET', 'OPTIONS']) {
      const request = new EventEmitter();
      Object.assign(request, {method, url: '/project-export?overleafProjectId=audit-document',
        headers: {host: 'attacker.example:31245'}});
      const result = await new Promise(resolve => server.emit('request', request, {
        status: 200, setHeader() {},
        writeHead(status) {this.status = status;},
        end(body) {resolve({status: this.status, body: body?.toString()});},
      }));
      assert.equal(result.status, 403);
      assert.match(result.body, /forbidden_host/);
    }
    assert.equal(exports, 0);
  } finally {
    server.emit('close');
    await fs.rm(tmp, {recursive: true, force: true});
  }
});

test('legitimate browser origins retain CORS and local preflight access', async () => {
  const tmp = await fs.mkdtemp(path.join(os.tmpdir(), 'lea-origin-boundary-'));
  const server = await createServer({settingsPath:path.join(tmp,'settings.json'), envPath:path.join(tmp,'.env'),
    jobsPath:path.join(tmp,'jobs.json'), chatSessionsPath:path.join(tmp,'chat.json'),env:{}});
  try {
    for (const origin of ['https://www.overleaf.com','chrome-extension://audit','moz-extension://audit',undefined,'https://attacker.example']) {
      const request=new EventEmitter();Object.assign(request,{method:'OPTIONS',url:'/statuses',headers:{host:'127.0.0.1:31245',...(origin?{origin}:{})}});
      const headers={};
      const result=await new Promise(resolve=>server.emit('request',request,{
        status:200,setHeader(key,value){headers[key]=value;},writeHead(status){this.status=status;},end(body){resolve({status:this.status,body});},
      }));
      assert.equal(result.status,204);
      if (!origin || origin==='https://attacker.example') assert.equal(headers['Access-Control-Allow-Origin'],undefined);
      if(origin && origin!=='https://attacker.example') assert.equal(headers['Access-Control-Allow-Origin'],origin);
      request.method='GET';request.url='/health';
      const getResult=await new Promise(resolve=>server.emit('request',request,{
        status:200,setHeader(){},writeHead(status){this.status=status;},end(){resolve(this.status);},
      }));
      assert.equal(getResult,origin==='https://attacker.example'?403:200);
    }
  } finally {server.emit('close');await fs.rm(tmp,{recursive:true,force:true});}
});
