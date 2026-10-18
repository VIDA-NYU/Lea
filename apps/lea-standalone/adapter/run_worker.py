"""The single workspace owner. API availability is independent of this process."""
import logging,os,signal,sys,threading
from pathlib import Path
os.environ['LEA_PROCESS_ROLE']='worker'
from app import runtime,process_state


def main():
    logging.basicConfig(level=logging.INFO)
    stop=threading.Event()
    class Lifetime:
        should_exit=False
        class config:timeout_graceful_shutdown=5
    lifetime=Lifetime()
    runtime.bind_server(lifetime)
    def terminate(*_):
        runtime.begin_drain(restart=False,force=True)
    signal.signal(signal.SIGINT,terminate);signal.signal(signal.SIGTERM,terminate)
    from app import worker
    try:
        worker.startup()
        capabilities=worker.health()['capabilities']
        from lea import execution
        capabilities['execution']=execution.capability()
        while not lifetime.should_exit:
            process_state.publish(runtime.IDENTITY,runtime.snapshot(),capabilities)
            stop.wait(1)
    finally:
        runtime.set_state('stopping')
        try:
            process_state.publish(runtime.IDENTITY,runtime.snapshot(),{})
            worker.shutdown_lean_checks()
        except Exception:logging.exception('Worker cleanup failed; process exit will fence remaining threads')
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
