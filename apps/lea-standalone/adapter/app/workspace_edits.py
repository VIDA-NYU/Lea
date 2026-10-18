"""Worker-owned manual edit/check/cascade; the companion only projects results."""
from pathlib import Path
from . import store, project_coordination, workspace_commands


def apply(session_id, request):
    # Route collaborators remain injectable at the existing integration seam;
    # ownership and the orchestration live here, never in an HTTP request.
    from .routes import sessions
    session=store.get_session(session_id)
    project_id=session.get('project_id') if session else None
    with project_coordination.file_edit(project_id):
        saved=sessions.write_file_session(session_id,request)
        if saved['unchanged']:
            return saved
        own=sessions.lean_check_session(session_id,sessions.PathRequest(path=request.path,formalization_id=request.formalization_id))
        result={**saved,'own_check':own,'dependents':[]}
        if not project_id:
            return result
        project=store.get_project(project_id)
        config=sessions.load_config()
        from .projects import project_repo_dir
        from .project_library import module_name,project_paths
        from lea.imports import direct_imports
        repo=project_repo_dir(project,config.lea_root/'workspace/proofs')
        with store.connect() as conn:
            rows=conn.execute('''select t.path,t.session_id,t.formalization_id from timeline t join sessions s on s.id=t.session_id
                where s.project_id=? and t.kind='code' and t.id in (select max(t2.id) from timeline t2 join sessions s2 on s2.id=t2.session_id
                    where s2.project_id=? and t2.kind='code' group by t2.path) limit 10001''',(project_id,project_id)).fetchall()
        if len(rows)>10000:
            raise ValueError('Cascade exceeds its 10000-file limit; submitted content is retained')
        entries={row['path']:dict(row) for row in rows}
        for absolute in project_paths(repo):
            relative=absolute.relative_to(repo).as_posix()
            entries.setdefault(relative,{'path':relative,'session_id':None,'formalization_id':None})
            if len(entries)>10000:raise ValueError('Cascade exceeds its 10000-file limit; submitted content is retained')
        imports={}
        total=0
        for path in entries:
            absolute=sessions.fs_service.safe_abs(repo,path)
            if absolute.is_file() and absolute.stat().st_size<=4*1024*1024:
                content=absolute.read_text();total+=len(content.encode())
                if total>32*1024*1024:raise ValueError('Cascade input exceeds 32 MiB; submitted content is retained')
                imports[path]=set(direct_imports(content))
        module=module_name(project,request.path)
        frontier={module};pending=set(imports)-{request.path};ordered=[]
        while pending:
            ready=sorted(path for path in pending if imports[path]&frontier)
            if not ready:break
            for path in ready:
                pending.remove(path);frontier.add(module_name(project,path));ordered.append(path)
        if not ordered:
            return result
        upstream=sessions.rebuild_session_module(session_id,sessions.PathRequest(path=request.path))
        for path in ordered:
            workspace_commands.authorize()
            row=entries[path]
            if row['session_id'] is None:
                result['dependents'].append({**row,'status':'unknown','detail':'No recorded editing context exists for this dependent.'})
                continue
            if upstream['status']!='ok':
                item={**row,'status':'unknown','detail':'Upstream module rebuild failed; dependent evidence is unavailable.'}
                step=store.current_proof_step(row['session_id'],path)
                if step:
                    store.add_code_step(row['session_id'],None,path,content=step['code'],author='environment',
                        check_status='error',check_detail=item['detail'],summary='Cascade unavailable after edit to '+request.path,
                        formalization_id=row['formalization_id'])
            else:
                built=sessions.rebuild_session_module(row['session_id'],sessions.PathRequest(path=path))
                if built['status']=='ok':
                    checked=sessions.lean_check_session(row['session_id'],sessions.PathRequest(path=path,formalization_id=row['formalization_id'],author='cascade',summary='Re-checked after edit to '+request.path))
                else:
                    # A warm LSP result cannot override a failed source build.
                    # Retain an attributed, unverified head rather than a stale
                    # positive check against a previously compiled dependency.
                    step=store.current_proof_step(row['session_id'],path)
                    if step:
                        store.add_code_step(row['session_id'],None,path,content=step['code'],author='environment',
                            check_status='error',check_detail=built.get('detail'),summary='Cascade build failed after edit to '+request.path,
                            formalization_id=row['formalization_id'])
                    checked={'status':'unavailable','detail':'Source build failed; warm results are not accepted.'}
                item={**row,**built,'check':checked}
            result['dependents'].append(item)
        return result
