"""SQL-fenced solver scheduler and independent control/editor consumers."""
from threading import Event,Thread
import logging
from . import commands,db

_stop=Event()
_threads=[]
_epoch=None
OWNER='lea-command-host'


def execute(token):
    import json
    if token['kind'].startswith('editor.'):
        from .editor_worker import execute as editor_execute
        return editor_execute(token)
    if not token['kind'].startswith('synthetic.'):
        from .command_execution import control
        return control(token)
    payload=json.loads(token['payload_json'])
    return {'text':payload['text'],'blob_id':payload.get('blob_id'),'kind':token['kind']}


def start():
    global _epoch
    if _threads:return
    _stop.clear()
    from . import editor_worker
    editor_worker.start()
    def supervise():
        global _epoch
        while not _stop.is_set():
            try:
                with db.connect() as conn:
                    enabled=conn.execute('select 1 from command_producers where enabled=1 limit 1').fetchone()
                if not enabled:
                    _stop.wait(.2);continue
                _epoch=commands.start_worker(OWNER)
                commands.recover()
                break
            except commands.QueueError:
                _stop.wait(1)
        if _stop.is_set():return
        def consume(lane):
            from . import run_commands
            with commands.readiness_reader() as ready:
                while not _stop.is_set():
                    try:
                        kinds=run_commands.SYNTHETIC_KINDS if lane=='compute' else {k for k in commands.REGISTRY if not k.startswith('companion.')}
                        token=commands.claim(OWNER,_epoch,lane,kinds=kinds) if ready(lane,kinds=kinds) else None
                        if token:
                            # Unknown-effect fixtures deliberately retain an expired
                            # claim for recovery classification, never run side effects.
                            if token['kind']!='synthetic.uncertain':
                                if token['kind'].startswith('editor.'):execute(token)
                                else:commands.complete(token,execute(token))
                            continue
                    except Exception:logging.getLogger(__name__).exception('Command consumer failed')
                    _stop.wait(.05 if lane=='editor' else .2 if lane=='control' else .5)
        for lane in ('compute','compute','control','editor'):
            worker=Thread(target=consume,args=(lane,),name='lea-command-'+lane,daemon=True)
            _threads.append(worker);worker.start()
        def workspace():
            from . import workspace_commands, runtime
            with commands.readiness_reader() as ready:
                while not _stop.is_set():
                    try:
                        if runtime.accepting_work() and ready('compute',kinds=workspace_commands.KINDS):
                            token=commands.claim(OWNER,_epoch,'compute',kinds=workspace_commands.KINDS)
                            if token:
                                try:
                                    with runtime.operation():workspace_commands.execute(token)
                                except RuntimeError as exc:
                                    if str(exc)!='adapter_draining':raise
                                    workspace_commands.defer_before_execution(token)
                                continue
                    except Exception:logging.getLogger(__name__).exception('Workspace command failed')
                    _stop.wait(.2)
        for number in range(2):
            worker=Thread(target=workspace,name='lea-command-workspace-'+str(number),daemon=True)
            _threads.append(worker);worker.start()
        def compatibility(lane):
            from . import companion_worker as companion_service,runtime
            kinds={'companion.control'} if lane=='control' else {'companion.query'} if lane=='editor' else {'companion.dispatch'}
            with commands.readiness_reader() as ready:
                while not _stop.is_set():
                    token=None
                    try:
                        if (lane=='control' or runtime.accepting_work()) and ready(lane,kinds=kinds):
                            token=commands.claim(OWNER,_epoch,lane,kinds=kinds)
                            if token:companion_service.execute(token);continue
                    except Exception as exc:
                        logging.getLogger(__name__).exception('Compatibility worker failed')
                        if token:commands.complete(token,error='compatibility_execution_uncertain')
                    _stop.wait(.2)
        for lane in ('compute','compute','editor','control'):
            worker=Thread(target=compatibility,args=(lane,),name='lea-compatibility-'+lane,daemon=True)
            _threads.append(worker);worker.start()
        def schedule():
            from .command_execution import solver_tick
            superseded={}
            while not _stop.is_set():
                try:solver_tick(OWNER,_epoch,superseded)
                except Exception:logging.getLogger(__name__).exception('Solver command scheduling failed')
                _stop.wait(.1)
        worker=Thread(target=schedule,name='lea-command-solvers',daemon=True)
        _threads.append(worker);worker.start()
        while not _stop.wait(5):
            try:
                from .command_execution import heartbeat_solvers
                from .run_commands import adopt_pending
                if not commands.heartbeat_worker(OWNER,_epoch):
                    logging.getLogger(__name__).error('Command worker lease expired; restart and reconcile before execution resumes')
                heartbeat_solvers(OWNER,_epoch)
                from .workspace_commands import heartbeat
                heartbeat(OWNER,_epoch)
                with db.connect() as conn:
                    tokens=[dict(r) for r in conn.execute("select * from commands where owner=? and worker_epoch=? and state='claimed' and kind like 'companion.%'",(OWNER,_epoch))]
                for token in tokens:commands.heartbeat(token)
                commands.recover()
                adopt_pending()
            except Exception:logging.getLogger(__name__).exception('Command recovery failed')
    worker=Thread(target=supervise,name='lea-command-supervisor',daemon=True)
    _threads.append(worker);worker.start()


def close():
    global _epoch
    _stop.set()
    from . import companion_worker as companion_service,editor_worker
    companion_service.close()
    editor_worker.close()
    for thread in list(_threads):
        if thread.ident is not None:thread.join(timeout=5)
        if thread.is_alive():raise RuntimeError('Command host did not stop')
    _threads.clear()
    if _epoch:
        with db.write() as conn:conn.execute('delete from command_workers where id=? and epoch=?',(OWNER,_epoch))
    _epoch=None
