import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';
import vm from 'node:vm';

test('project observations refresh settled proof facts without a session timestamp change and fence old responses',async()=>{
  const compiled=await build({entryPoints:[new URL('./components/ProjectWindow.tsx',import.meta.url).pathname],
    bundle:true,write:false,platform:'node',format:'cjs',jsx:'automatic',plugins:[{name:'project-view',setup(b){
      b.onResolve({filter:/^(react(?:\/jsx-runtime)?|lucide-react)$|^\.\//},a=>
        a.kind==='entry-point'?null:{path:a.path,namespace:'fake'});
      b.onResolve({filter:/^\.\.\/(lib\/api|stores\/proofSession)$/},a=>({path:a.path,namespace:'fake'}));
      b.onLoad({filter:/.*/,namespace:'fake'},a=>({contents:a.path==='react'
        ? 'exports.useState=x=>globalThis.state(x);exports.useMemo=f=>f();exports.useEffect=(f,d)=>globalThis.effects.push({f,d});'
        : a.path==='react/jsx-runtime' ? 'exports.jsx=exports.jsxs=(type,props)=>({type,props});exports.Fragment="fragment";'
        : a.path.endsWith('proofSession') ? 'exports.useProofSession=f=>f({formalizationRefreshToken:globalThis.token});'
        : a.path.endsWith('lib/api') ? 'exports.listProjectFormalizations=()=>globalThis.request();'
        : 'export const ChevronLeft=()=>null,Download=()=>null,MarkdownDoc=()=>null,FilesCard=()=>null,BlueprintTab=()=>null,FilesystemTab=()=>null,SkillsMcpTab=()=>null;'}));
    }}]});
  const requests=[],values=[];let stateIndex=0,form=false;
  const context={module:{exports:{}},token:0,effects:[],state:initial=>{
    const i=stateIndex++;return [i===0&&!form?'formalizations':initial,value=>{values[i]=value;}];
  },request:()=>new Promise(resolve=>requests.push(resolve))};context.exports=context.module.exports;context.globalThis=context;
  vm.runInNewContext(compiled.outputFiles[0].text,context);
  const props={project:{id:'p',title:'P',namespace:'Lea.P',sessions:[{updated_at:'2026-01-01'}]},onBack(){},onStartProof(){},onStartFormalization(){},onOpenSession(){}};
  const find=node=>!node||typeof node!=='object'?null:node.type?.name==='FormalizationsTab'?node:
    (Array.isArray(node)?node: [node.props?.children]).flat().map(find).find(Boolean);
  const render=()=>{form=false;stateIndex=0;return find(context.module.exports.ProjectWindow(props));};
  const before=render();context.token++;const after=render();
  assert.notEqual(before.props.refreshSignal,after.props.refreshSignal);
  form=true;stateIndex=0;before.type(before.props);const old=context.effects.at(-1).f();
  old();stateIndex=0;after.type(after.props);context.effects.at(-1).f();
  requests[1]({formalizations:[{id:'checked',validity_status:'proved'}],summary:{proved:1}});
  await Promise.resolve();requests[0]({formalizations:[{id:'stale'}],summary:{unchecked:1}});await Promise.resolve();
  assert.equal(values[0][0].id,'checked');assert.equal(values[1].proved,1);
});
