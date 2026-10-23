"""Database-only HTTP process. Restarting it never drains workspace execution."""
import argparse,os,signal
import uvicorn
GRACEFUL_SHUTDOWN_SECONDS=3

class DrainingServer(uvicorn.Server):
    def handle_exit(self,sig,frame):
        self.config.app.state.shutting_down=True
        if self.should_exit and sig==signal.SIGINT:
            self.force_exit=True
            os._exit(130)
            return
        super().handle_exit(sig,frame)
LeaServer=DrainingServer

def create_server(app,*,host='127.0.0.1',port=8001,log_level='debug'):
    app.state.shutting_down=False
    return DrainingServer(uvicorn.Config(app,host=host,port=port,loop='asyncio',http='h11',
        log_level=log_level,timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS))

def main(argv=None):
    os.environ['LEA_PROCESS_ROLE']='api'
    from app.api_boundary import install
    install()
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',default='127.0.0.1');parser.add_argument('--port',type=int,default=8001)
    parser.add_argument('--log-level',default='debug');args=parser.parse_args(argv)
    from app.main import app
    server=create_server(app,host=args.host,port=args.port,log_level=args.log_level)
    server.run()
    if not server.started:raise RuntimeError('API startup failed')
if __name__=='__main__':main()
