"""Private queued language worker. Browser traffic never reaches an LSP session.

Only fixed methods below may execute. Each private server is owned, confined,
versioned and bounded; its output can never enter canonical proof-check tables.
"""
from collections import OrderedDict
from datetime import datetime,timezone
import json
import hashlib
from pathlib import Path
from queue import Empty
from threading import RLock,Event
import tempfile
import time
from . import commands,db,editor_service as service

_pool=OrderedDict()
_lock=RLock()
_closing=Event()
MAX_SESSIONS=4
IDLE_SECONDS=60
METHODS={'editor.hover':'textDocument/hover','editor.complete':'textDocument/completion',
         'editor.goals':'$/lean/plainGoal','editor.term_goal':'$/lean/plainTermGoal'}


def _target(buffer):
    from .config import load_config
    from . import projects,filesystem,store
    config=load_config()
    if not config.lea_root:raise RuntimeError('workspace_unavailable')
    with db.connect() as conn:
        owner=conn.execute('select scope from editor_clients where id=?',(buffer['owner_id'],)).fetchone()
    session=store.get_session(owner[0][2:] if owner and owner[0].startswith('s:') else buffer['session_id'])
    project=store.get_project(session['project_id']) if session and session['project_id'] else None
    if not session:raise RuntimeError('scope_deleted')
    repo=projects.repo_for_session(session,config.lea_root/'workspace/proofs',project)
    path=filesystem.safe_abs(repo,buffer['path'])
    workspace=(config.lea_root/'workspace').resolve()
    for root in (path.parent,*path.parent.parents):
        if not root.is_relative_to(workspace):break
        if (root/'lakefile.lean').is_file() or (root/'lakefile.toml').is_file():return path,root
    raise RuntimeError('lake_project_unavailable')


class Session:
    def __init__(self,root,budget):
        from lea.lsp_daemon import LeanDaemon
        from lea import execution
        class EditorDaemon(LeanDaemon):
            def _format(self,path,diagnostics):return diagnostics
            def _dispatch(self,message):
                if message and message.get('method') is not None and message.get('id') is None:
                    # Plain language features need only the latest diagnostic
                    # publication, not an unbounded stream of progress/widget events.
                    if message['method']!='textDocument/publishDiagnostics':return
                    with self._state_lock:
                        queue=self._uri_queues.get((message.get('params') or {}).get('uri'))
                        if queue:
                            try:
                                while queue.qsize()>1:queue.get_nowait()
                            except Empty:pass
                super()._dispatch(message)
        self.temporary=tempfile.TemporaryDirectory(prefix='lea-editor-')
        self.policy={'writable_roots':[self.temporary.name],'temporary_root':self.temporary.name}
        self.daemon=EditorDaemon(str(root))
        self.last_used=time.monotonic()
        self.document=None
        with execution.scope(self.policy):
            if not self.daemon.start(budget):
                self.close()
                raise self.daemon.failure or RuntimeError('editor_worker_unavailable')

    def close(self):
        self.daemon.shutdown()
        self.temporary.cleanup()

    def query(self,path,content,kind,position,budget):
        from lea import execution
        with execution.scope(self.policy):
            identity=(str(path),hashlib.sha256(content.encode()).hexdigest())
            if self.document!=identity:
                self.diagnostics=self.daemon.check(str(path),content,budget=budget)
                self.document=identity
            diagnostics=self.diagnostics
            if kind=='editor.analyze':return {'diagnostics':diagnostics}
            params={'textDocument':{'uri':path.as_uri()},'position':position}
            result=self.daemon._request(METHODS[kind],params,budget.remaining(),budget=budget)
            if kind=='editor.complete':
                # Never execute commands or apply edits to other files from an LSP result.
                items=result if isinstance(result,list) else (result or {}).get('items',[])
                keys={'label','kind','detail','documentation','insertText','insertTextFormat','textEdit','filterText','sortText'}
                return {'isIncomplete':bool(len(items)>200 or isinstance(result,dict) and result.get('isIncomplete')),
                    'items':[{k:v for k,v in item.items() if k in keys} for item in items[:200]]}
            return result


def _close_idle(now):
    for key,session in list(_pool.items()):
        if now-session.last_used>IDLE_SECONDS or session.daemon.broken:
            _pool.pop(key);session.close()


def start():_closing.clear()


def close():
    _closing.set()
    with _lock:
        for session in _pool.values():session.close()
        _pool.clear()


def maintain():
    if _lock.acquire(blocking=False):
        try:_close_idle(time.monotonic())
        finally:_lock.release()


def analyze(buffer,request,content,should_stop,timeout):
    from lea.check_runtime import CheckBudget,CheckFailure
    from lea import check_evidence,execution
    budget=CheckBudget(timeout,should_stop=should_stop)
    path,root=_target(buffer)
    # The language process may evaluate project macros, so it gets an owned temp
    # write root, no network, and no permission to change the working tree.
    if not execution.capability()['available']:raise RuntimeError('confined_editor_executor_unavailable')
    with _lock:
        _close_idle(time.monotonic())
        key=(buffer['owner_id'],str(root),buffer['path'],buffer['environment_revision'])
        session=_pool.get(key)
        # Exact environment manifests detect imported artifacts/configuration even
        # before the autonomous SQL observer notices an external edit.
        with tempfile.TemporaryDirectory(prefix='lea-editor-inputs-') as scratch:
            with execution.scope({'writable_roots':[scratch],'temporary_root':scratch}):
                with check_evidence.coordinator(root).lease(budget=budget):
                    manifest=check_evidence.manifest(path,content,root,budget)
                    fingerprint=check_evidence.environment_hash(manifest)
                    if session and getattr(session,'environment',None)!=fingerprint:
                        _pool.pop(key);session.close();session=None
                    if not session:
                        while len(_pool)>=MAX_SESSIONS:
                            _,old=_pool.popitem(last=False);old.close()
                        session=Session(root,budget);session.environment=fingerprint;_pool[key]=session
                    try:
                        value=session.query(path,content,request['kind'],json.loads(request['position_json']) if request['position_json'] else None,budget)
                        budget.remaining()
                        if any(check_evidence.file_observation(root/name)[0]!=sha for name,sha in manifest['configuration'].items()) or any(check_evidence.file_observation(record['path'])[0]!=record['sha256'] for record in manifest['files'].values()):
                            raise CheckFailure('environment_changed','Language environment changed during analysis',retryable=False)
                    except Exception:
                        _pool.pop(key,None);session.close();raise
                    session.last_used=time.monotonic();_pool.move_to_end(key)
                    return {'value':value,'environment_sha256':fingerprint,'worker_generation':session.daemon.generation,
                        'analysis_only':True,'elapsed_ms':round((time.monotonic()-budget.started)*1000,2)}


def execute(token):
    started=time.monotonic()
    with db.read_snapshot() as conn:
        request=conn.execute('select * from editor_requests where command_id=?',(token['id'],)).fetchone()
        buffer=conn.execute('select b.*,v.content from editor_buffers b join editor_contents v on v.id=b.blob_id where b.id=?',(request['buffer_id'],)).fetchone() if request else None
    if not request or not buffer:
        commands.complete(token,error='editor_buffer_expired');return
    request,buffer=dict(request),dict(buffer)
    cached_at=0;stopped=False
    def should_stop():
        nonlocal cached_at,stopped
        if time.monotonic()-cached_at>.05:
            with db.read_snapshot() as conn:
                stopped=_closing.is_set() or not commands._owned(conn,token) or bool(service.current(conn,buffer))
            cached_at=time.monotonic()
        return stopped
    failure=None;result=None
    try:
        if should_stop():raise RuntimeError('editor_request_superseded')
        left=(datetime.fromisoformat(token['deadline'])-datetime.now(timezone.utc)).total_seconds()
        result=analyze(buffer,request,buffer['content'],should_stop,max(.001,left-.025))
        if len(json.dumps(result,allow_nan=False,ensure_ascii=False).encode())>service.RESULT_BYTES:raise RuntimeError('editor_result_too_large')
    except Exception as exc:
        failure=getattr(exc,'kind',None) or str(exc)[:160] or 'editor_worker_unavailable'
    with db.write() as conn:
        row=commands._owned(conn,token)
        if not row:return
        reason=service.current(conn,buffer)
        if reason:failure=reason
        if not failure:
            used=conn.execute('select coalesce(sum(length(cast(result_json as blob))),0) from editor_requests where owner_id=?',(buffer['owner_id'],)).fetchone()[0]
            if used+len(commands._dump(result).encode())>service.RESULT_OWNER_BYTES:failure='editor_result_quota'
        if not failure:
            conn.execute("update editor_requests set result_json=?,result_state='available' where command_id=?",(commands._dump(result),token['id']))
        commands._settle(conn,row,'failed' if failure else 'succeeded',{'editor_request':token['id'],'elapsed_ms':round((time.monotonic()-started)*1000,2)},failure)
