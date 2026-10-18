"""Process-test worker: real lifecycle/queue, scripted model events only."""
import os,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
os.environ['LEA_PROCESS_ROLE']='worker'
from app import bridge
from lea.interface import TurnStarted,AssistantTextDelta,Finished
from lea.providers import Usage

def events(config,messages,*,should_stop=None,**kwargs):
    yield TurnStarted(1)
    yield AssistantTextDelta('Scripted process fixture. ')
    marker=Path(os.environ['LEA_TEST_RELEASE'])
    while not marker.exists() and not (should_stop and should_stop()):time.sleep(.05)
    yield Finished('interrupted' if should_stop and should_stop() else 'completed','Fixture complete.',1,None,config.model,
        Usage(input_tokens=1,output_tokens=1),0.0,{'messages':[]},result_kind='answered')
bridge.run_events=events
if os.environ.get('LEA_TEST_OWNED_CHILD'):
    from app import worker
    original_startup=worker.startup
    def startup():
        original_startup()
        import subprocess,json
        from lea import process_ownership
        lease=process_ownership.prepare()
        child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(3600)'],
            start_new_session=True,pass_fds=(lease.fd,))
        process_ownership.register(child.pid,lease)
        process_ownership.release_parent(lease,spawned=True)
        Path(os.environ['LEA_TEST_OWNED_CHILD']).write_text(json.dumps({'pid':child.pid,'lease':str(lease.path)}))
    worker.startup=startup
from run_worker import main
code=0
try:main()
except BaseException:
    import traceback
    traceback.print_exc();code=1
finally:os._exit(code)
