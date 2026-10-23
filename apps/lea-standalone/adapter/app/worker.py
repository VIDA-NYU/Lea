"""Private workspace lifecycle. Imported only by run_worker.py."""

from __future__ import annotations

from . import peripheral_views

import logging
import os
from pathlib import Path

from .routes import (workspace, observation, project_library, alignment_checks, lea_status, overleaf_source, formalize_batch_reports, custom_tools, formalizations, mcp_servers, projects, runs, search,
                     sessions, settings, skills, subagents)
from . import db, alignment_store, bridge, formalize_batch_reports as batch_report_service, github_import_service, store, recovery, runtime, observation_maintenance, command_host
from .config import load_config


_startup_log = logging.getLogger("lea.worker")
def startup() -> None:
    runtime.set_state("starting")
    runtime.acquire_data_lock()
    try:
        config=load_config()
        workspace=(config.lea_root or Path(__file__).resolve().parents[2]/'prover')/'workspace'
        runtime.acquire_workspace_lock(workspace)
        from lea import process_ownership
        # Reap the pre-split data-directory manifest as well as the new shared
        # physical-workspace manifest before accepting a new worker epoch.
        os.environ['LEA_OWNED_PROCESS_DIR']=str(db.DB_PATH.parent/'owned-processes')
        process_ownership.reap_previous()
        if (workspace/'.lea-owned-processes').resolve()!=runtime.owned_process_dir().resolve():
            os.environ['LEA_OWNED_PROCESS_DIR']=str(workspace/'.lea-owned-processes')
            process_ownership.reap_previous()
        os.environ["LEA_OWNED_PROCESS_DIR"] = str(runtime.owned_process_dir())
        _startup_log.info("Startup: cleaning up previously owned workers.")
        process_ownership.reap_previous()
        _startup_log.info("Startup: preparing the database.")
        from .bootstrap import require_schema
        require_schema()
        from . import process_state
        process_state.record_start(runtime.IDENTITY,os.getpid())
        from . import settings_policy
        settings_policy.bootstrap()
        from .companion_import import import_directory
        from .companion_worker import ROOT as monorepo_root
        import_directory(Path(os.environ.get('LEA_COMPANION_IMPORT_DIR',monorepo_root/'apps/overleaf-extension/.overleaf-lean-stub')))
        # The data lock and owned-process reaper establish a new process lifetime.
        # Old SQL leases cannot authorize acceptance during journal recovery.
        with db.write() as conn:
            conn.execute("delete from command_workers where id='lea-command-host'")
        from . import workspace_files, workspace_effects
        workspace_files.recover()
        workspace_effects.recover()
        store.interrupt_message_streams()
        runtime.set_state("recovering")
        _startup_log.info("Startup: recovering interrupted work.")
        from .reformalization_apply import recover as recover_applications
        recover_applications()
        recovery.recover_running()
        from . import reformalizations
        reformalizations.recover_operations()
        alignment_store.recover_interrupted()
        batch_report_service.recover_interrupted()
        bridge.recover_runs_at_startup()
        from . import workspace_commands
        workspace_commands.recover_at_startup()
        from .project_dependencies import recover
        recover()
        from . import project_library
        projects = store.list_projects()
        _startup_log.info("Startup: indexing source files in %d projects; compiler checks deferred.", len(projects))
        for project in projects:
            project_library.refresh(project, resolve_environment=False)
            # One-time legacy registry adoption belongs to startup maintenance,
            # never to the artifact-index query.
            from .routes.projects import _ensure_artifacts_backfilled
            _ensure_artifacts_backfilled(project)
            from .companion_import import import_registry
            import_registry(project,load_config().lea_root or Path(__file__).resolve().parents[2]/'prover')
        config = load_config()
        proofs_root = config.lea_root / "workspace" / "proofs" if config.lea_root else None
        _startup_log.info("Startup: recovering interrupted GitHub imports.")
        github_import_service.recover_github_imports_at_startup(proofs_root)
        from .catalog_service import service as catalog_service
        catalog_service().start()
        runtime.set_state("ready")
        from . import peripheral_views
        for kind in ('get_settings','models'):
            peripheral_views.capture(kind,{})
        capabilities=health()['capabilities']
        from lea import execution
        capabilities['execution']=execution.capability()
        process_state.publish(runtime.IDENTITY,runtime.snapshot(),capabilities)
        observation_maintenance.start()
        command_host.start()
    except BaseException:
        command_host.close()
        observation_maintenance.close()
        raise


def health() -> dict:
    from . import commands,editor_service
    from .process_state import BOUNDARY
    from .lea_status import admission_enabled
    from .reformalizations import capability
    from .release_info import identity
    return {"ok": True, **runtime.snapshot(), "capabilities": {"lea_status": {"version": 1, "admission_enabled": admission_enabled(), "independent_checks": False, "source_pause_policy": 1}, "overleaf_source_refresh": {"version": 2,'publication_version':1,'callbacks':False}, "project_library": {"version": 1, "dependency_coordination": True}, 'reformalization': capability(),
        'database_boundary':BOUNDARY,'release':identity(),
        'core_projections':{'version':1,'enrollment':'automatic','observation_lifetime_seconds':120},
        'editor':editor_service.capability(),
        'commands':commands.capability()}}


def shutdown_lean_checks() -> None:
    command_host.close()
    observation_maintenance.close()
    from .catalog_service import service as catalog_service
    catalog_service().close()
    from lea.lsp_daemon import _shutdown_all
    from lea.mcp import shutdown_pool
    try:
        try:
            _shutdown_all()
        finally:
            shutdown_pool()
    finally:
        pass  # The process retains its workspace lock until exit, including daemon threads.

