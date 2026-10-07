// Audit reproduction: delayed API response with isolated stores.
import { build } from 'esbuild';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);

const bundled = await build({
  entryPoints: [fileURLToPath(new URL('../../apps/lea-standalone/src/app/hooks/useProofStream.ts', import.meta.url))],
  bundle: true, write: false, platform: 'node', format: 'cjs',
  plugins: [{ name: 'audit-fakes', setup(b) {
    b.onResolve({filter: /^react$/}, () => ({path: 'react', namespace: 'audit'}));
    b.onResolve({filter: /^\.\.\/lib\/api$/}, () => ({path: 'api', namespace: 'audit'}));
    b.onResolve({filter: /^\.\.\/stores\/(proofSession|sessions)$/}, a => ({path: a.path.split('/').pop(), namespace: 'audit'}));
    b.onLoad({filter: /.*/, namespace: 'audit'}, a => ({ contents: a.path === 'react'
      ? 'exports.useRef = x => ({current:x}); exports.useEffect = () => {};'
      : a.path === 'api' ? 'exports.getSession = id => globalThis.getSession(id);'
      : `exports.use${a.path === 'proofSession' ? 'ProofSession' : 'Sessions'} = globalThis.${a.path};` }));
  }}]
});
const proofState = {formalizationScope:'project', runStatus:undefined, messages:[], codeSteps:[]};
const proofProxy = new Proxy(proofState, {get(t,k) {
  if (k === 'resetSessionScoped') return () => {};
  if (typeof k === 'string' && k.startsWith('set')) {
    const field = k[3].toLowerCase()+k.slice(4);
    return x => {t[field] = typeof x === 'function' ? x(t[field]) : x;};
  }
  return t[k];
}});
const sessionState = {selectedSessionId:'A', setSelectedSessionId(id) {this.selectedSessionId=id;}, refreshSessions:async () => []};
let resolveA;
const context = {module:{exports:{}}, exports:{}, require, setTimeout, clearTimeout,
  proofSession:{getState:() => proofProxy}, sessions:{getState:() => sessionState},
  getSession:() => new Promise(resolve => {resolveA=resolve;}), EventSource:class {},
};
context.exports = context.module.exports;
context.globalThis = context;
vm.runInNewContext(bundled.outputFiles[0].text, context);
const hook = context.module.exports.useProofStream();
const detail = id => ({id,messages:[],code_steps:[],runs:[],formalizations:[]});
const pending = hook.reconcile('A');
hook.applyDetail(detail('B'));
console.log('selected_after_switch:', sessionState.selectedSessionId);
resolveA(detail('A'));
await pending;
console.log('selected_after_old_check_resolves:', sessionState.selectedSessionId);
