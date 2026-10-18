"""Fail-closed API boundary, installed before route imports in every API launch.

The operating-system deployment boundary is the separate API container without
workspace/config mounts. This audit guard additionally rejects accidental local
imports, filesystem access, child processes and outgoing connections. It is not
an isolation boundary against malicious Python injected into the API process.
"""
import importlib.abc
import os
from pathlib import Path
import sys

PRIVATE={'worker','runtime','bridge','command_host','command_execution','editor_worker','companion_worker',
    'recovery','workspace_effects','workspace_files','proof_files','proof_evidence','project_library','project_dependencies',
    'reformalizations','reformalization_apply','gitstore','filesystem','uploads','projects','settings','catalog_service',
    'runbroker','runregistry','observation_maintenance','core_observer','workspace_observer','artifact_snapshots','target_completion','github_import_service','formalize_batch_reports','formalizations','lea_status'}
_installed=False

class PrivateImportDenied(ImportError):pass

class PublicImports(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if (fullname.split('.')[0] in {'lea','litellm','ctypes'} or fullname=='app.routes' or fullname.startswith('app.routes.')
            or fullname in {'app.'+name for name in PRIVATE}):
            raise PrivateImportDenied('API execution import denied: '+fullname)


def install():
    global _installed
    if _installed:return
    if os.environ.get('LEA_PROCESS_ROLE')=='test' and 'pytest' in sys.modules:return
    os.environ['LEA_PROCESS_ROLE']='api'
    sys.dont_write_bytecode=True
    guard=PublicImports()
    for name in tuple(sys.modules):guard.find_spec(name)
    from . import db
    adapter=Path(__file__).resolve().parents[1]
    application=adapter.parent
    root=application.parents[1] if application.parent.name=='apps' else application
    data=db.DB_PATH.parent.resolve()
    static=Path(os.environ['LEA_WEB_DIST']).resolve() if os.environ.get('LEA_WEB_DIST') else None
    private_roots=[application/'prover',application/'config',root/'apps/overleaf-extension/.overleaf-lean-stub',data/'owned-processes']
    if os.environ.get('LEA_API_DENY_PATHS'):
        private_roots.extend(Path(p).resolve() for p in os.environ['LEA_API_DENY_PATHS'].split(os.pathsep) if p)
    library_roots=[Path(sys.prefix).resolve(),Path(sys.base_prefix).resolve(),adapter,
        Path('/System/Library'),Path('/usr/share/zoneinfo'),Path('/etc')]
    library_roots=[path.resolve() for path in library_roots]
    def within(path,parent):return path==parent or parent in path.parents
    def check(raw,write=False):
        if isinstance(raw,int) or raw is None:return
        path=Path(os.fsdecode(raw)).absolute().resolve(strict=False)
        if any(within(path,parent) for parent in private_roots):raise PermissionError('API workspace/config access denied: '+str(path))
        if within(path,data):return
        if not write and (path in {Path('/dev/null'),Path('/dev/urandom')} or static and within(path,static) or any(within(path,parent) for parent in library_roots)):return
        raise PermissionError('API filesystem access denied: '+str(path))
    def audit(event,args):
        if event=='open':
            mode=args[1];flags=args[2]
            check(args[0],bool(isinstance(mode,str) and any(c in mode for c in 'wax+') or isinstance(flags,int) and flags&(os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC)))
        elif event=='sqlite3.connect':
            raw=str(args[0])
            if raw.startswith('file:'):raw=raw[5:].split('?',1)[0]
            if Path(raw).resolve()!=db.DB_PATH.resolve():raise PermissionError('API database path denied')
        elif event in {'os.listdir','os.scandir'}:check(args[0])
        elif event in {'os.remove','os.rmdir','os.mkdir','os.chmod','os.truncate','os.utime'}:check(args[0],True)
        elif event in {'os.rename','os.link','os.symlink'}:check(args[0],True);check(args[1],True)
        elif event in {'subprocess.Popen','os.system','os.posix_spawn','os.fork','os.exec','socket.connect','socket.getaddrinfo'}:
            raise PermissionError('API execution/outgoing transport denied: '+event)
    # Credentials are accepted only as bounded infrastructure tickets. They are
    # neither loaded from the worker config nor inherited by the API process.
    for name in list(os.environ):
        if name.endswith('_API_KEY') or any(x in name for x in ('AUTH_TOKEN','GITHUB_TOKEN','PASSWORD','SECRET')):os.environ.pop(name,None)
    scratch=data/'http-intake';scratch.mkdir(parents=True,exist_ok=True,mode=0o700)
    import tempfile
    tempfile.tempdir=str(scratch)
    sys.meta_path.insert(0,guard)
    sys.addaudithook(audit)
    _installed=True
