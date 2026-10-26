"""The single workspace owner. API availability is independent of this process."""
import logging,os,signal,sys,threading,sqlite3
from pathlib import Path
os.environ['LEA_PROCESS_ROLE']='worker'
from app import runtime,process_state
from app import worker_diagnostics


def publish_status(capabilities):
    """A temporary observation write failure must not kill active solvers.

    SQL expiry and command leases remain authoritative during an outage. An
    obsolete lifetime and non-contention failures still fail closed.
    """
    try:
        # Public health derives queue counts from SQL. Avoid an unbounded read
        # before the heartbeat's bounded write, especially behind a busy COMMIT.
        process_state.publish(runtime.IDENTITY, lambda: {**runtime.snapshot(include_queue=False),
            'diagnostics': worker_diagnostics.snapshot()}, capabilities)
        return True
    except sqlite3.OperationalError as exc:
        code = getattr(exc, 'sqlite_errorcode', 0) & 0xff
        if code not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
            raise
        worker_diagnostics.event('worker.status_write_delayed', severity='warning',
                                 exception=type(exc).__name__, message=str(exc))
        return False


def main():
    logging.basicConfig(level=logging.INFO)
    worker_diagnostics.setup(runtime.IDENTITY)
    stop=threading.Event()
    class Lifetime:
        should_exit=False
        class config:timeout_graceful_shutdown=5
    lifetime=Lifetime()
    runtime.bind_server(lifetime)
    def terminate(signum, *_):
        worker_diagnostics.event('worker.signal', signal=signal.Signals(signum).name)
        runtime.begin_drain(restart=False,force=True)
    signal.signal(signal.SIGINT,terminate);signal.signal(signal.SIGTERM,terminate)
    from app import worker
    try:
        worker.startup()
        capabilities=worker.health()['capabilities']
        from lea import execution
        capabilities['execution']=execution.capability()
        worker_diagnostics.event('worker.ready')
        while not lifetime.should_exit:
            publish_status(capabilities)
            stop.wait(1)
    except BaseException as exc:
        worker_diagnostics.exception('worker.exception', exc)
        raise
    finally:
        runtime.set_state('stopping')
        try:
            publish_status({})
        except Exception as exc:
            worker_diagnostics.exception('worker.final_status_failed', exc)
        try:
            worker.shutdown_lean_checks()
        except Exception as exc:
            worker_diagnostics.exception('worker.cleanup_failed', exc)
        worker_diagnostics.event('worker.exiting', restart=runtime.restart_requested())
        worker_diagnostics.close()
    if runtime.restart_requested():
        # Release no filesystem lock until all Python threads are gone. Exec
        # closes CLOEXEC descriptors and the next lifetime reaps owned children.
        os.execv(sys.executable,[sys.executable,str(Path(__file__).resolve())])

if __name__=='__main__':
    code=0
    try:main()
    except BaseException:
        logging.exception('Worker exited with an error');code=1
    finally:os._exit(code)
