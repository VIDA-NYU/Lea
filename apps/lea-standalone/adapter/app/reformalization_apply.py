"""Recoverable, exact-path publication of a checked reformalization.

Only this host routine writes canonical retry sources. Displaced files remain on
the same filesystem until their bytes are durable in artifact_blobs. An arbitrary
editor can still write through an old descriptor; observed divergence is retained
as a conflict and never resolved with a repository reset.
"""
from __future__ import annotations

import json
import os
import shutil
from copy import deepcopy
from pathlib import Path
import tempfile
from uuid import uuid4

from lea import execution, interface
from lea.runctx import run_context, current_run_key, current_should_stop
from lea.check_evidence import freshness
from lea.result_selection import inspect_file, select, ALLOWED_AXIOMS
from . import db, store, project_coordination, reformalizations as operations
from .gitstore import GitStore
from .reformalization_scope import validate_project, PreservationConflict
from .reformalization_scope import bounds


def _write_journal(journal_id, state, manifest, commit=None):
    with db.write() as conn:
        conn.execute("update reformalization_applications set state=?,manifest_json=?,commit_sha=coalesce(?,commit_sha),updated_at=? where id=?",
                     (state, operations.dumps(manifest), commit, db.utc_now(), journal_id))


def _content(blob):
    return store.blob_content(blob) if blob else None


def _path(repo, relative):
    path = repo / relative
    if path.is_symlink() or not path.resolve().is_relative_to(repo.resolve()) or any(p.startswith('.') for p in Path(relative).parts):
        raise PreservationConflict('Unsafe publication path: ' + relative)
    return path


def _install(path, content, mode=0o644):
    """Hard-link a complete sibling temporary file: destination is create-only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.lea-install-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(content.encode())
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temp, mode)
        os.link(temp, path)  # EEXIST never overwrites an editor's new destination.
    finally:
        os.unlink(temp)


def _retain_displaced(entry, displaced):
    if not displaced.exists():
        return
    if displaced.is_symlink():
        raise PreservationConflict('An external editor replaced a source with a symlink')
    observed = displaced.read_bytes().decode('utf-8')
    with db.write() as conn:
        entry['displaced_blob'] = store._put_blob(conn, observed)
    entry['displaced_sha256'] = operations.digest(observed)


def _stage_assessment(op, selection, *, contents=None, target_name=None):
    manifest = op['manifest']
    stage = Path(manifest['repo'])
    before = {p: _content(v['blob_id']) for p, v in manifest['files'].items()}
    after = contents if contents is not None else {p: f.read_bytes().decode('utf-8') for p, f in operations.lean_files(stage).items()}
    changed = {p: after.get(p) for p in before.keys() | after.keys() if before.get(p) != after.get(p)}
    reports, checks = {}, {}
    policy = operations.workspace_policy(manifest['workspace'])
    with run_context(working_dir=str(stage), run_key=current_run_key(),
                     should_stop=current_should_stop()), execution.scope(policy):
        for rel in sorted(after):
            path = _path(stage, rel)
            checked = interface.check(str(path), cold=True)
            checks[rel] = checked
            for entry in ((checked.execution or {}).get('environment') or {}).get('files', {}).values():
                dependency = Path(entry['path']).resolve()
                workspace = Path(manifest['workspace'])
                if entry.get('kind') == 'source' and dependency.is_relative_to(workspace) and not dependency.is_relative_to(stage):
                    canonical_dependency = Path(manifest['canonical_root']) / dependency.relative_to(workspace)
                    if not canonical_dependency.is_file() or operations.digest(canonical_dependency.read_bytes()) != entry['sha256']:
                        raise PreservationConflict('A newly used dependency differs from this attempt copy')
            try:
                reports[rel] = inspect_file(path, allow_errors=True)
            except ValueError:
                reports[rel] = None
        selected_path = selection['path']
        report = reports.get(selected_path)
        if not report or checks[selected_path].status != 'ok':
            raise PreservationConflict('The selected result module has no passing Lean check')
        chosen = select(report, selection['declaration'], selection.get('kind'))
        chosen['outcome'] = selection.get('outcome') or selection.get('kind')
        used_paths = {selected_path}
        for dependency in ((checks[selected_path].execution or {}).get('environment') or {}).get('files', {}).values():
            path = Path(dependency['path']).resolve()
            if dependency.get('kind') == 'source' and path.is_relative_to(stage):
                used_paths.add(path.relative_to(stage).as_posix())
        # New unimported exploration files remain durable drafts. They are not
        # part of the result's publication merely because a tool wrote them last.
        after = {p: code for p, code in after.items() if p in before or p in used_paths}
        reports = {p: r for p, r in reports.items() if p in after}
        checks = {p: c for p, c in checks.items() if p in after}
        changed = {p: after.get(p) for p in before.keys() | after.keys() if before.get(p) != after.get(p)}
        source_update = bool(manifest.get('source_update')) and not target_name
        form = store.get_formalization(op['formalization_id'])
        with db.connect() as conn:
            pinned = [r[0] for r in conn.execute("select declaration_name from project_dependencies where owner_run_id=?", (op['root_run_id'],))]
        scope = validate_project(before, after, manifest['reports'], reports,
            target_name or form['declaration_name'], chosen, source_update=source_update, pinned_helpers=pinned)
        if source_update:
            scope['source_evidence'] = manifest['source_update']
            previous = next(d for r in manifest['reports'].values() if r for d in r['declarations'] if d['user_name'] == scope['target_before'])
            scope['contract_change'] = {'before': previous['type'], 'after': chosen['type'],
                'before_type': previous['type_expr'], 'after_type': chosen['type_expr'],
                'before_body': previous.get('body_expr'), 'after_body': chosen.get('body_expr')}
        impact = [p for p, c in checks.items() if c.status != 'ok' and manifest['checks'].get(p, {}).get('status') == 'ok']
        if impact and not source_update:
            raise PreservationConflict('The candidate breaks previously passing consumers: ' + ', '.join(impact))
        if any(_path(stage, path).read_bytes().decode('utf-8') != content for path, content in after.items()):
            raise PreservationConflict('Attempt sources changed while the compiler was checking them; the draft is retained')
    return {'before': before, 'after': after, 'changes': changed, 'scope': scope,
            'selection': {**selection, 'declaration': chosen['user_name']},
            'reports': reports, 'repair_needed': impact, 'workspace': manifest['workspace'],
            'checks': {p: {'status': c.status, 'detail': c.detail, 'execution': c.execution} for p, c in checks.items()}}


def assess(operation_id, selection):
    op = operations.get(operation_id)
    operations.fresh(op)
    try:
        return _stage_assessment(op, selection)
    except PreservationConflict as exc:
        m = op['manifest']
        if not m.get('source_update'):
            raise
        selection = _extract_source_target(op, selection)
        return _stage_assessment(op, selection)


def _extract_source_target(op, selection):
    """Separate an independently checkable changed contract from retained consumers."""
    m = op['manifest']
    stage = Path(m['repo'])
    path = _path(stage, selection['path'])
    code = path.read_bytes().decode('utf-8')
    with execution.scope(operations.workspace_policy(m['workspace'])):
        report = inspect_file(path, allow_errors=True)
        chosen = select(report, selection['declaration'], selection.get('kind'))
    local = {d['user_name']: d for d in report['declarations']}
    needed, pending = set(), [chosen['user_name']]
    while pending:
        name = pending.pop()
        if name in needed:
            continue
        needed.add(name)
        pending.extend(n for n in local.get(name, {}).get('dependencies', []) if n in local)
    protected = {d['user_name'] for r in m['reports'].values() if r for d in r['declarations']
                 if d['user_name'] != chosen['user_name'] and 'sorryAx' not in d.get('axioms', [])}
    if any(local[n]['user_name'] in protected for n in needed):
        raise PreservationConflict('The changed target needs a protected same-module declaration; retain the draft for a checked relocation')
    spans = {bounds(code, d) for d in report['declarations'] if d['user_name'] in needed}
    extracted = code
    for lo, hi in sorted({bounds(code, d) for d in report['declarations']} - spans, reverse=True):
        extracted = extracted[:lo] + extracted[hi:]
    retained = code
    for lo, hi in sorted(spans, reverse=True):
        retained = retained[:lo] + retained[hi:]
    relative = 'Reformalized/' + chosen['user_name'].rsplit('.', 1)[-1] + '_' + op['id'].replace('-', '')[:12] + '.lean'
    destination = _path(stage, relative)
    if destination.exists():
        raise PreservationConflict('The extraction destination is occupied')
    module = m['namespace'] + '.' + relative[:-5].replace('/', '.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    _install(destination, extracted)
    # Imports precede namespaces and every retained command. The compiler checks
    # this compatibility module without rewriting any dependent proof bytes.
    path.write_text('import ' + module + '\n' + retained)
    return {**selection, 'path': relative}


def _canonical_checks(op, journal):
    m = op['manifest']
    repo, root = Path(m['canonical_repo']), Path(m['canonical_root'])
    selection = journal['selection']
    checks, reports = {}, {}
    # Finished is yielded while the agent's run context is still active. Bind
    # trusted host checks to the actual repo, retaining cancellation and identity,
    # without widening the agent's workspace or its subprocess write policy.
    with run_context(working_dir=str(repo), run_key=current_run_key(),
                     should_stop=current_should_stop()), execution.scope(operations.canonical_policy(root, repo)):
        # Check all effective project consumers, including previously failing ones.
        # prepared_environment rebuilds local imports from the current source.
        for rel, path in sorted(operations.lean_files(repo).items()):
            result = interface.check(str(path), cold=True)
            report = None
            try:
                report = inspect_file(path, allow_errors=True)
            except ValueError as exc:
                inspection_error = str(exc)
            evidence = dict(result.execution or {})
            proposed_evidence = journal.get('candidate_checks', {}).get(rel, {}).get('execution') or {}
            def source_inputs(execution, staged=False):
                values = {}
                for item in (execution.get('environment') or {}).get('files', {}).values():
                    if item.get('kind') != 'source':
                        continue
                    path = Path(item['path']).resolve()
                    candidate_root = Path(journal.get('candidate_workspace') or m['workspace'])
                    if staged and path.is_relative_to(candidate_root):
                        path = root / path.relative_to(candidate_root)
                    values[str(path)] = item['sha256']
                return values
            if result.status == 'ok' and proposed_evidence and source_inputs(proposed_evidence, True) != source_inputs(evidence):
                raise PreservationConflict('The actual canonical import context differs from the checked candidate: ' + rel)
            reports[rel] = report
            evidence['selected_results'] = [d for d in (report or {}).get('declarations', [])
                if not (set(d.get('axioms', [])) - ALLOWED_AXIOMS)]
            if rel == selection['path']:
                if result.status != 'ok' or not report:
                    raise PreservationConflict('The actual canonical result failed Lean: ' + str(result.detail or (report or {}).get('diagnostic') or locals().get('inspection_error')))
                evidence['selected_result'] = select(report, selection['declaration'], selection.get('kind'))
            if result.status != 'ok' and m['checks'].get(rel, {}).get('status') == 'ok' and not journal['scope']['source_update']:
                raise PreservationConflict('The actual canonical consumer failed: ' + rel
                    + ': ' + str(result.detail or 'Unknown compiler error'))
            if not evidence.get('environment_sha256') or not freshness(evidence, path)[0]:
                raise PreservationConflict('Canonical check evidence is missing or stale: ' + rel)
            checks[rel] = {'status': result.status, 'detail': result.detail,
                           'execution': evidence, 'content': result.content}
        before = {p: _content(v['blob_id']) for p, v in m['files'].items()}
        after = {p: c['content'] for p, c in checks.items()}
        chosen = checks[selection['path']]['execution']['selected_result']
        proposed = select(journal['reports'][selection['path']], selection['declaration'], selection.get('kind'))
        if any(chosen.get(k) != proposed.get(k) for k in ('kind', 'type_expr', 'body_expr', 'visibility')):
            raise PreservationConflict('The actual canonical result differs from the checked candidate contract')
        chosen['outcome'] = selection.get('outcome') or selection.get('kind')
        target = (journal.get('helper') or {}).get('declaration_name') or store.get_formalization(op['formalization_id'])['declaration_name']
        with db.connect() as conn:
            pinned = [r[0] for r in conn.execute('select declaration_name from project_dependencies where owner_run_id=?', (op['root_run_id'],))]
        journal['scope'] = {**journal['scope'], **validate_project(before, after, m['reports'], reports, target, chosen,
            source_update=journal['scope']['source_update'], pinned_helpers=pinned)}
        journal['reports'] = reports
        # Keep the existing optional audit policy. An unavailable binary is never
        # represented as successful replay evidence.
        audit = interface.verify(str(repo / selection['path']), declaration=selection['declaration'])
        if audit.status in {'rejected', 'error'}:
            raise PreservationConflict('Selected kernel audit did not pass: ' + str(audit.detail))
        journal['audit'] = {'status': audit.status, 'detail': audit.detail,
                            'execution': audit.execution}
        if journal.get('helper') and audit.status != 'ok':
            raise PreservationConflict('Independent helper publication requires its kernel audit')
        journal['helper_results'] = []
        if not journal.get('helper'):
            from .project_library import semantic_key
            with db.connect() as conn:
                owned = [dict(r) for r in conn.execute('select * from project_dependencies where owner_run_id=?', (op['root_run_id'],))]
            for dep in owned:
                report = reports.get(dep['path'])
                if not report or checks[dep['path']]['status'] != 'ok':
                    continue
                try:
                    chosen_helper = select(report, dep['declaration_name'])
                except ValueError:
                    continue
                if semantic_key(chosen_helper) != dep['semantic_key']:
                    raise PreservationConflict('A joint helper changed its pinned contract')
                helper_audit = interface.verify(str(repo / dep['path']), declaration=dep['declaration_name'])
                if helper_audit.status == 'ok':
                    journal['helper_results'].append({'dependency': dep, 'result': {**chosen_helper,
                        'certified': True, 'reusable': True, 'path': dep['path'], 'check_status': 'ok',
                        'verification_status': 'ok', 'content_hash': chosen_helper['content_sha256']}})
        from .project_library import environment_hash, semantic_key
        audited_names = {h['result']['user_name'] for h in journal['helper_results']}
        if journal['audit']['status'] == 'ok':
            audited_names.add(selection['declaration'])
        journal['library'] = {}
        for rel, report in reports.items():
            if not report:
                continue
            declarations = [{**d, 'semantic_key': semantic_key(d),
                'audit_status': 'ok' if d['user_name'] in audited_names else None} for d in report['declarations']]
            journal['library'][rel] = {'environment_hash': environment_hash(store.get_project(op['project_id']), repo, repo / rel),
                                      'declarations': declarations}
    operations.fresh(op, applied={p: _content(e['after_blob']) for p, e in journal['files'].items()})
    return checks


def _accept(op, journal_id, journal, commit):
    """One authoritative transaction: bytes, checks, mappings, outcome, receipt."""
    now = db.utc_now()
    selection = journal['selection']
    fid, sid, rid = (journal.get('helper') or {}).get('formalization_id', op['formalization_id']), op['session_id'], op['root_run_id']
    project = store.get_project(op['project_id'])
    with db.write() as conn:
        if journal.get('workspace_command'):
            from . import commands
            if not commands._owned(conn,journal['workspace_command']):
                raise PreservationConflict('The workspace command no longer owns publication')
        if journal.get('command_token'):
            from . import run_commands
            current=conn.execute('select stop_requested_reason from runs where id=?',(rid,)).fetchone()
            if not current or current[0] or not run_commands.owns(conn,rid,journal['command_token']):
                raise PreservationConflict('The solver command was cancelled or fenced before publication')
        operation_state = conn.execute('select state,request_json from reformalization_operations where id=?', (op['id'],)).fetchone()
        if (operation_state['state'] not in {'active', 'applying', 'worker'}
                or json.loads(operation_state['request_json']).get('_cancelled')):
            raise PreservationConflict('The operation was cancelled before acceptance')
        approved = (op.get('result') or {}).get('approval_id')
        if approved and conn.execute("select 1 from formalization_review_events where action='withdraw' and approval_id=?", (approved,)).fetchone():
            raise PreservationConflict('The reviewed candidate approval was withdrawn before acceptance')
        state = conn.execute('select state from reformalization_applications where id=?', (journal_id,)).fetchone()
        if state and state['state'] == 'published':
            return
        previous_by_path = {path: conn.execute("select t.id,t.after_blob_id from timeline t join sessions s on s.id=t.session_id where s.project_id=? and t.kind='code' and t.path=? order by t.id desc limit 1", (op['project_id'], path)).fetchone()
                            for path in op['manifest']['files']}
        for rel, check in journal['checks'].items():
            code = check['content']
            blob = store._put_blob(conn, code)
            previous = previous_by_path.get(rel)
            before_blob = op['manifest']['files'].get(rel, {}).get('blob_id')
            evidence = check['execution']
            eid = evidence.get('check_id') or str(uuid4())
            data = {'reformalization_application': journal_id,
                    'preservation': {'from_step_id': str(previous['id']) if previous and previous['after_blob_id'] == before_blob else None,
                        'before_blob_id': before_blob,
                        'protected': journal['scope']['protected'], 'source_update': journal['scope']['source_update'],
                        'stable_protected': journal['scope']['stable_protected']}}
            relocated = []
            for move in journal['scope'].get('relocations', []):
                if move['after_path'] != rel:
                    continue
                prior_blob = op['manifest']['files'][move['before_path']]['blob_id']
                parent = previous_by_path.get(move['before_path'])
                relocated.append({**move, 'before_blob_id': prior_blob,
                    'from_step_id': str(parent['id']) if parent and parent['after_blob_id'] == prior_blob else None})
            data['preservation']['relocations'] = relocated
            step = conn.execute("""insert into timeline(session_id,run_id,kind,author,path,after_blob_id,
                summary,formalization_id,created_at,content_lost,data,check_status,check_detail,
                check_evidence_id,artifact_kind) values (?,?,'code','agent',?,?,?,?,?,0,?,?,?,?,?)""",
                (sid, rid, rel, blob, 'Reformalization application', fid if rel == selection['path'] else None,
                 now, operations.dumps(data), check['status'], check['detail'], eid,
                 ('definition' if selection.get('kind') == 'defined' else 'proof') if check['status'] == 'ok' else None))
            conn.execute("""insert into proof_check_evidence(id,session_id,project_id,run_id,code_step_id,
                blob_id,path,status,detail,evidence_json,created_at) values (?,?,?,?,?,?,?,?,?,?,?)""",
                (eid, sid, op['project_id'], rid, step.lastrowid, blob, rel, check['status'], check['detail'], operations.dumps(evidence), now))
            if rel == selection['path']:
                audit = journal.get('audit') or {}
                conn.execute("""insert into verification_events(id,formalization_id,session_id,run_id,
                    code_step_id,path,status,detail,created_at,blob_id,evidence_json) values (?,?,?,?,?,?,?,?,?,?,?)""",
                    (str(uuid4()), fid, sid, rid, step.lastrowid, rel, audit.get('status', 'unavailable'), audit.get('detail'),
                     now, blob, operations.dumps(audit.get('execution')) if audit.get('execution') else None))
            library = journal.get('library', {}).get(rel)
            if library:
                conn.execute("""insert into project_library_modules(project_id,path,source_hash,
                    environment_hash,declarations_json,build_status,updated_at,check_evidence_json)
                    values (?,?,?,?,?,?,?,?) on conflict(project_id,path) do update set source_hash=excluded.source_hash,
                    environment_hash=excluded.environment_hash,declarations_json=excluded.declarations_json,
                    build_status=excluded.build_status,verify_status=null,updated_at=excluded.updated_at,check_evidence_json=excluded.check_evidence_json""",
                    (op['project_id'], rel, operations.digest(code), library['environment_hash'], operations.dumps(library['declarations']), check['status'], now, operations.dumps(evidence)))
        conn.execute("update formalization_files set role='support',updated_at=? where formalization_id=? and role='primary'", (now, fid))
        for rel in journal['checks']:
            if rel in journal['repair_needed'] and rel != selection['path']:
                conn.execute('delete from formalization_files where formalization_id=? and path=?', (fid, rel))
                continue
            if rel == selection['path'] or rel in journal['files']:
                conn.execute("""insert into formalization_files(formalization_id,path,role,created_at,updated_at)
                    values (?,?,?,?,?) on conflict(formalization_id,path) do update set role=excluded.role,updated_at=excluded.updated_at""",
                    (fid, rel, 'primary' if rel == selection['path'] else 'support', now, now))
        conn.execute("delete from artifacts where formalization_id=? and declaration_name<>?", (fid, selection['declaration']))
        for move in journal['scope'].get('relocations', []):
            candidates = conn.execute("""select f.*,ff.role from formalizations f join formalization_files ff
                on ff.formalization_id=f.id where f.project_id=? and ff.path=?""", (op['project_id'], move['before_path'])).fetchall()
            old_report = op['manifest']['reports'][move['before_path']]
            from .reformalization_scope import resolve
            for form in candidates:
                try:
                    named = resolve(old_report, form['declaration_name'])
                except (ValueError, TypeError):
                    continue
                if named['user_name'] != move['name']:
                    continue
                conn.execute('delete from formalization_files where formalization_id=? and path=?', (form['id'], move['before_path']))
                conn.execute("""insert into formalization_files(formalization_id,path,role,created_at,updated_at)
                    values (?,?,?,?,?) on conflict(formalization_id,path) do update set role=excluded.role,updated_at=excluded.updated_at""",
                    (form['id'], move['after_path'], form['role'], now, now))
                conn.execute('update artifacts set path=?,module_name=?,updated_at=? where formalization_id=? and path=?',
                    (move['after_path'], project['namespace'] + '.' + move['after_path'][:-5].replace('/', '.'), now, form['id'], move['before_path']))
        conn.execute("""insert into artifacts(id,scope,project_id,session_id,run_id,declaration_name,kind,
            path,module_name,formalization_id,source_hash,created_at,updated_at) values (?,?,?,?,?,?,?,?,?,?,?,?,?)
            on conflict(scope,declaration_name) do update set session_id=excluded.session_id,
            run_id=excluded.run_id,path=excluded.path,module_name=excluded.module_name,
            formalization_id=excluded.formalization_id,source_hash=excluded.source_hash,updated_at=excluded.updated_at""",
            (str(uuid4()), op['project_id'], op['project_id'], sid, rid, selection['declaration'],
             'definition' if selection.get('kind') == 'defined' else 'theorem', selection['path'],
             project['namespace'] + '.' + selection['path'][:-5].replace('/', '.'), fid,
             (journal.get('helper') or {}).get('source_hash', op['manifest']['source_hash']), now, now))
        receipt = {'application_id': journal_id, 'commit_sha': commit, 'outcome': journal['outcome'],
                   'selected_result': selection, 'repair_needed': journal['repair_needed'], 'preservation': journal['scope'],
                   'approval_id': (op.get('result') or {}).get('approval_id')}
        if journal.get('helper'):
            dep = journal['helper']
            chosen = journal['checks'][selection['path']]['execution']['selected_result']
            from .project_library import semantic_key
            if semantic_key(chosen) != dep['semantic_key']:
                raise PreservationConflict('The actual canonical helper violates its pinned contract')
            result = {**chosen, 'reusable': True, 'certified': True, 'path': selection['path'],
                'check_status': 'ok', 'verification_status': journal.get('audit', {}).get('status'),
                'content_hash': chosen['content_sha256']}
            changed = conn.execute("update project_dependencies set state='ready',owner_run_id=null,result_json=?,blocker_json=null,updated_at=? where id=? and owner_run_id=? and generation=?",
                         (operations.dumps(result), now, dep['id'], rid, dep['generation'])).rowcount
            if not changed:
                raise PreservationConflict('Helper ownership changed before acceptance')
            conn.execute("update project_dependency_attempts set state='ready',updated_at=? where dependency_id=? and generation=?", (now, dep['id'], dep['generation']))
            baseline = op['manifest']
            for rel, entry in journal['files'].items():
                if entry['after_blob']:
                    baseline['files'][rel] = {'blob_id': entry['after_blob'], 'sha256': operations.digest(_content(entry['after_blob'])), 'mode': entry['mode']}
                else:
                    baseline['files'].pop(rel, None)
                key = str((Path(baseline['canonical_repo']) / rel).relative_to(Path(baseline['canonical_root'])))
                if entry['after_blob']:
                    baseline['source_hashes'][key] = operations.digest(_content(entry['after_blob']))
                else:
                    baseline['source_hashes'].pop(key, None)
                baseline['reports'][rel] = journal['reports'].get(rel)
                baseline['checks'][rel] = {'status': journal['checks'][rel]['status']}
            child = bool(op['request'].get('_foreground_owner'))
            conn.execute("update reformalization_operations set state=?,manifest_json=?,result_json=?,updated_at=? where id=?", ('settled' if child else 'active', operations.dumps(baseline), operations.dumps(receipt) if child else None, now, op['id']))
            if child:
                conn.execute('update runs set status=?,result_kind=?,result_detail=? where id=?', (journal['outcome'], journal['outcome'], journal['outcome'].upper(), rid))
        elif not approved:
            conn.execute("update runs set status=?,result_kind=?,result_detail=? where id=?",
                         (journal['outcome'], journal['outcome'], journal['outcome'].upper(), rid))
        conn.execute("update reformalization_applications set state='published',manifest_json=?,commit_sha=?,updated_at=? where id=?",
                     (operations.dumps(journal), commit, now, journal_id))
        if not journal.get('helper'):
            conn.execute("update reformalization_operations set state='settled',result_json=?,updated_at=? where id=?",
                         (operations.dumps(receipt), now, op['id']))
        for helper in journal.get('helper_results', []):
            dep = helper['dependency']
            conn.execute("update project_dependencies set state='ready',owner_run_id=null,result_json=?,blocker_json=null,updated_at=? where id=? and owner_run_id=? and generation=?",
                (operations.dumps(helper['result']), now, dep['id'], rid, dep['generation']))
            conn.execute("update project_dependency_attempts set state='ready',updated_at=? where dependency_id=? and generation=?", (now, dep['id'], dep['generation']))
            conn.execute("""insert into artifacts(id,scope,project_id,session_id,run_id,declaration_name,kind,path,module_name,formalization_id,source_hash,created_at,updated_at)
                values (?,?,?,?,?,?,?,?,?,?,?,?,?) on conflict(scope,declaration_name) do update set run_id=excluded.run_id,path=excluded.path,updated_at=excluded.updated_at""",
                (str(uuid4()), op['project_id'], op['project_id'], sid, rid, dep['declaration_name'], helper['result']['kind'], dep['path'], dep['module_name'], dep['formalization_id'], dep['source_hash'], now, now))
    store._bump_sessions_changed()


def restore(op, journal_id, journal, *, committed=False):
    repo = Path(op['manifest']['canonical_repo'])
    _write_journal(journal_id, 'restoring', journal)
    conflict = False
    restored = {}
    for rel, entry in reversed(list(journal['files'].items())):
        path = _path(repo, rel)
        current = path.read_bytes().decode('utf-8') if path.exists() else None
        before, after = _content(entry['before_blob']), _content(entry['after_blob'])
        displaced = Path(entry['displaced'])
        _retain_displaced(entry, displaced)
        observed = _content(entry.get('displaced_blob'))
        rollback = Path(entry['rollback_displaced']) if entry.get('rollback_displaced') else None
        if rollback and rollback.exists():
            captured = {}
            _retain_displaced(captured, rollback)
            entry['rollback_blob'] = captured.get('displaced_blob')
            rolled = _content(entry['rollback_blob'])
            if rolled != after:
                conflict = True
                if current is None and rolled is not None:
                    try:
                        _install(path, rolled, entry['mode'])
                    except FileExistsError:
                        pass
                continue
        # Both an untouched path and a completed per-path restore are safe.
        # A crash before displacement leaves the original untouched.
        if current == before and (observed is None or observed == before):
            restored[rel] = before
            continue
        if current not in (after, None) or (observed is not None and observed != before):
            conflict = True
            if current is not None:
                with db.write() as conn:
                    entry['conflicting_blob'] = store._put_blob(conn, current)
            if current == after and current is not None and observed != before:
                archived = displaced.with_name(displaced.name + '.candidate-' + uuid4().hex)
                entry['rollback_displaced'] = str(archived)
                _write_journal(journal_id, 'restoring', journal)
                os.rename(path, archived)
                captured = {}
                _retain_displaced(captured, archived)
                entry['rollback_displaced'] = str(archived)
                entry['rollback_blob'] = captured.get('displaced_blob')
                _write_journal(journal_id, 'restoring', journal)
                # Retain an editor's replacement arriving in the rename gap too.
                if _content(entry['rollback_blob']) != after:
                    observed = _content(entry['rollback_blob'])
                current = None
            # If the path is vacant, put the observed displaced edit back.
            if current is None and observed is not None:
                try:
                    _install(path, observed, entry['mode'])
                except FileExistsError:
                    pass
            continue
        if current is not None:
            rollback = displaced.with_name(displaced.name + '.candidate-' + uuid4().hex)
            entry['rollback_displaced'] = str(rollback)
            _write_journal(journal_id, 'restoring', journal)
            os.rename(path, rollback)
            rolled = {}
            _retain_displaced(rolled, rollback)
            entry['rollback_displaced'] = str(rollback)
            entry['rollback_blob'] = rolled.get('displaced_blob')
            _write_journal(journal_id, 'restoring', journal)
            if _content(entry['rollback_blob']) != after:
                conflict = True
                try:
                    _install(path, _content(entry['rollback_blob']), entry['mode'])
                except FileExistsError:
                    pass
                continue
        if before is not None:
            try:
                _install(path, before, entry['mode'])
            except FileExistsError:
                conflict = True
        restored[rel] = before
    if committed and restored and not conflict:
        GitStore(repo.parent).commit_exact(repo, 'Restore interrupted reformalization', restored, application_id=journal_id + '-restore')
    journal['restored'] = not conflict
    _write_journal(journal_id, 'conflict' if conflict else 'restored', journal)
    return not conflict


def apply(operation_id, selection, outcome, *, assessment=None, helper=None,command_token=None):
    op = operations.get(operation_id)
    # Acquire publication ownership before preparing any durable journal. A
    # blocked contender has neither changed files nor left recovery work behind.
    with operations.acting(operation_id), project_coordination.application(
            op['project_id'], op['manifest']['canonical_root']):
        receipt = _apply(operation_id, selection, outcome, assessment=assessment, helper=helper,command_token=command_token)
        if helper and op['request'].get('_foreground_owner'):
            from .reformalization_helpers import refresh_parent
            refresh_parent(operation_id)
        return receipt


def _apply(operation_id, selection, outcome, *, assessment=None, helper=None,command_token=None):
    op = operations.get(operation_id)
    if op['state'] not in {'active', 'worker'}:
        raise PreservationConflict('This operation no longer owns publication')
    manifest = op['manifest']
    repo, root = Path(manifest['canonical_repo']), Path(manifest['canonical_root'])
    assessment = assessment or assess(operation_id, selection)
    journal_id = str(uuid4())
    journal = {k: assessment[k] for k in ('scope', 'selection', 'repair_needed')}
    journal.update(outcome=outcome, files={}, checks={}, reports=assessment['reports'], helper=helper,
                   candidate_checks=assessment['checks'], candidate_workspace=assessment['workspace'],
                   approval_id=(op.get('result') or {}).get('approval_id'))
    if command_token:
        journal['command_token']={k:command_token[k] for k in ('id','owner','worker_epoch','generation','attempt_id')}
    from .workspace_commands import current as workspace_command, authorize
    if workspace_command():
        journal['workspace_command']={k:workspace_command()[k] for k in ('id','owner','worker_epoch','generation','attempt_id')}
    with db.write() as conn:
        authorize(conn)
        if command_token:
            from . import run_commands
            if not run_commands.owns(conn,op['root_run_id'],command_token):
                raise PreservationConflict('The solver command no longer owns publication')
        for rel, content in assessment['changes'].items():
            before = manifest['files'].get(rel)
            journal['files'][rel] = {'before_blob': before['blob_id'] if before else None,
                'after_blob': store._put_blob(conn, content) if content is not None else None,
                'mode': before['mode'] if before else 0o644,
                'displaced': str(repo / '.lea/displaced' / journal_id / rel)}
        conn.execute("insert into reformalization_applications(id,operation_id,state,manifest_json,created_at,updated_at) values (?,?,'prepared',?,?,?)",
                     (journal_id, operation_id, operations.dumps(journal), db.utc_now(), db.utc_now()))
    commit = None
    try:
        operations.fresh(op)
        operations.update(operation_id, 'worker' if op['request'].get('_foreground_owner') else 'applying')
        _write_journal(journal_id, 'applying', journal)
        for rel, entry in journal['files'].items():
            path = _path(repo, rel)
            displaced = Path(entry['displaced'])
            displaced.parent.mkdir(parents=True, exist_ok=True)
            before = _content(entry['before_blob'])
            if before is not None:
                os.rename(path, displaced)
                _retain_displaced(entry, displaced)
                _write_journal(journal_id, 'applying', journal)
                if _content(entry.get('displaced_blob')) != before:
                    raise PreservationConflict('An external edit was captured during application: ' + rel)
            if entry['after_blob']:
                _install(path, _content(entry['after_blob']), entry['mode'])
            entry['installed'] = True
            _write_journal(journal_id, 'applying', journal)
        journal['checks'] = _canonical_checks(op, journal)
        for entry in journal['files'].values():
            displaced = Path(entry['displaced'])
            if displaced.exists():
                _retain_displaced(entry, displaced)
                if _content(entry['displaced_blob']) != _content(entry['before_blob']):
                    raise PreservationConflict('A displaced source changed before acceptance; all observed versions are retained')
        _write_journal(journal_id, 'applying', journal)
        commit = GitStore(repo.parent).commit_exact(repo, 'Reformalize selected result', assessment['changes'], application_id=journal_id)
        _write_journal(journal_id, 'applying', journal, commit)
        _accept(op, journal_id, journal, commit)
    except BaseException as exc:
        # Process death recovery sees the pre-written journal. Ordinary failures
        # restore observed operation-owned paths immediately.
        if isinstance(exc, Exception):
            journal['failure'] = str(exc)
            if not commit:
                commit = GitStore(repo.parent)._git(repo, 'log', '-1', '--format=%H', '--fixed-strings', '--grep', 'Reformalization-Application: ' + journal_id).strip() or None
            restored = restore(op, journal_id, journal, committed=bool(commit))
            if helper and restored:
                operations.update(operation_id, 'worker' if op['request'].get('_foreground_owner') else 'active')
            else:
                operations.update(operation_id, 'retained', result={'outcome': 'conflict', 'detail': str(exc), 'application_id': journal_id})
        raise
    return operations.get(operation_id)['result']


def recover():
    """No solver calls. Complete a committed, identical apply or restore its paths."""
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute("select * from reformalization_applications where state in ('prepared','applying','restoring') order by created_at")]
    for row in rows:
        op = operations.get(row['operation_id'])
        journal = json.loads(row['manifest_json'])
        repo, root = Path(op['manifest']['canonical_repo']), Path(op['manifest']['canonical_root'])
        with operations.acting(op['id']), project_coordination.application(op['project_id'], root, recovery=True):
            gs = GitStore(repo.parent)
            commit = row['commit_sha']
            if not commit:
                commit = gs._git(repo, 'log', '-1', '--format=%H', '--fixed-strings', '--grep', 'Reformalization-Application: ' + row['id']).strip() or None
                if not commit and not journal['files']:
                    commit = gs._git(repo, 'rev-parse', 'HEAD').strip()
            try:
                if not commit or row['state'] == 'restoring':
                    raise PreservationConflict('Interrupted before durable acceptance')
                journal['checks'] = _canonical_checks(op, journal)
                _accept(op, row['id'], journal, commit)
            except Exception as exc:
                journal['failure'] = str(exc)
                restore(op, row['id'], journal, committed=bool(commit))
                operations.update(op['id'], 'retained', result={'outcome': 'conflict', 'detail': str(exc), 'application_id': row['id']})

    # Acceptance can commit immediately before a crash. Reconcile its parent
    # stage before admissions restart, even though its journal is now published.
    from .reformalization_helpers import refresh_parent
    with db.connect() as conn:
        children = [r[0] for r in conn.execute("""select distinct o.id from reformalization_operations o
            join reformalization_applications a on a.operation_id=o.id
            where a.state='published' and o.request_json like '%_foreground_owner%'""")]
    for child_id in children:
        child = operations.get(child_id)
        with operations.acting(child_id), project_coordination.application(
                child['project_id'], child['manifest']['canonical_root'], recovery=True):
            refresh_parent(child_id)


def publish_helper(op, dep, report, selected):
    """Build a helper-only candidate, never the parent's unfinished target bytes."""
    operations.fresh(op)
    baseline = op['manifest']
    stage = Path(baseline['repo'])
    relative = dep['path']
    code = (stage / relative).read_bytes().decode('utf-8')
    by_name = {d['user_name']: d for d in report['declarations']}
    needed, pending = set(), [selected['user_name']]
    while pending:
        name = pending.pop()
        if name in needed:
            continue
        needed.add(name)
        pending.extend(n for n in by_name.get(name, {}).get('dependencies', []) if n in by_name)
    old_code = _content(baseline['files'].get(relative, {}).get('blob_id'))
    old_report = baseline['reports'].get(relative)
    if old_code is None:
        helper_code = code
        spans = {bounds(code, d) for d in report['declarations'] if d['user_name'] not in needed}
        keep = {bounds(code, by_name[n]) for n in needed}
        for lo, hi in sorted(spans - keep, reverse=True):
            helper_code = helper_code[:lo] + helper_code[hi:]
    elif old_report:
        old = {d['user_name']: d for d in old_report['declarations']}
        replacements, additions = {}, []
        for name in needed:
            d = by_name[name]
            lo, hi = bounds(code, d)
            if d['user_name'] in old:
                previous = old[d['user_name']]
                if previous.get('type_expr') != d.get('type_expr'):
                    raise PreservationConflict('An inline helper changed its pinned contract')
                if 'sorryAx' in previous.get('axioms', []):
                    replacements[bounds(old_code, previous)] = code[lo:hi]
            else:
                additions.append(code[lo:hi])
        helper_code = old_code
        for (lo, hi), replacement in sorted(replacements.items(), reverse=True):
            helper_code = helper_code[:lo] + replacement + helper_code[hi:]
        if additions:
            # Insert within the target's established namespace/context. Compiler
            # inspection of this independent candidate must confirm the names.
            from .reformalization_scope import resolve
            target = resolve(old_report, store.get_formalization(op['formalization_id'])['declaration_name'])
            at, _ = bounds(helper_code, target)
            helper_code = helper_code[:at] + '\n'.join(additions) + '\n' + helper_code[at:]
    else:
        raise PreservationConflict('No safe inline helper source boundary')
    helper_root = Path(baseline['workspace']).parent / ('helper-' + str(uuid4()))
    helper_repo = helper_root / stage.relative_to(Path(baseline['workspace']))
    helper_repo.mkdir(parents=True)
    canonical_root = Path(baseline['canonical_root'])
    for name in baseline['configuration']:
        shutil.copy2(canonical_root / name, helper_root / name)
    for rel in baseline['source_hashes']:
        dest = helper_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(canonical_root / rel, dest)
    (helper_root / '.lake').mkdir(exist_ok=True)
    packages = canonical_root / '.lake/packages'
    if packages.exists():
        (helper_root / '.lake/packages').symlink_to(packages.resolve(), target_is_directory=True)
    (helper_repo / relative).parent.mkdir(parents=True, exist_ok=True)
    (helper_repo / relative).write_text(helper_code)
    helper_op = deepcopy(op)
    helper_op['manifest'].update(repo=str(helper_repo), workspace=str(helper_root))
    selection = {'path': relative, 'declaration': dep['declaration_name'],
                 'kind': 'defined' if selected['kind'] != 'theorem' else 'proved'}
    assessment = _stage_assessment(helper_op, selection, target_name=dep['declaration_name'])
    from .run_commands import current_token
    apply(op['id'], selection, selection['kind'], assessment=assessment, helper=dep,command_token=current_token())
    return {'outcome': 'published', 'dependency': project_dependencies_get(dep['id'])}


def project_dependencies_get(dependency_id):
    from .project_dependencies import get
    return get(dependency_id)
