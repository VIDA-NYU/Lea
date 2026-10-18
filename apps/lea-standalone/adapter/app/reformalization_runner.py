"""Attempt-scoped bridge. Canonical publication is a separate host action."""
from __future__ import annotations

from dataclasses import replace, asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from threading import Event

from lea import execution
from lea.events import (AssistantTextDelta, TurnStarted, FileChanged, CheckResult,
    Finished, UsageUpdated, ToolCalled, ToolApprovalRequested, Diagnostic,
    LeaStatusUpdateRequested, LeaStatusUpdateAck, ProjectLibraryRequested,
    ProjectLibraryAck, DependencyReadinessRequested, DependencyReadinessAck)
from lea.events import SubagentStarted, SubagentProgress, SubagentFinished
from . import store, recovery, runbroker, reformalizations as operations


def _relative(raw, repo):
    path = Path(raw)
    path = (path if path.is_absolute() else repo / path).resolve()
    if not path.is_relative_to(repo):
        raise ValueError('An attempt event points outside its workspace')
    return path.relative_to(repo).as_posix()


def _capture(op, run_id, repo):
    latest_by_path = {d['path']: d for d in operations.drafts(op['id'])}
    bound = {f['path'] for f in (op.get('manifest') or {}).get('associations', [])}
    for rel, path in operations.lean_files(repo).items():
        code = path.read_bytes().decode('utf-8')
        latest = latest_by_path.get(rel)
        if latest and latest['code'] == code:
            continue
        baseline = (op.get('manifest') or {}).get('files', {}).get(rel)
        if not latest and rel not in bound and baseline and operations.digest(code) == baseline['sha256']:
            continue
        operations.save_draft(op['id'], run_id, rel, code)


def run(context, op):
    from . import bridge, projects, lea_status, lea_status_store, project_dependencies
    from .reformalization_apply import assess, apply
    sid, rid, events = context.session_id, context.run_id, context.events
    stop = bridge._stop_events.setdefault(rid, Event())
    attempt = None
    repo = None
    status, kind, detail, reason, recoverable = 'failed', 'failed', None, None, False
    usage = bridge._UsageByTurn()
    turn, incoming, outgoing, cost, revision = 0, 0, 0, 0.0, 0
    narration = []
    children = {}
    skill_temp = None
    started = time.monotonic()
    initial = store.get_run(rid) or {}
    deadline = datetime.fromisoformat(initial['deadline_at']) if initial.get('deadline_at') else None
    def preparation_stopped():
        if not stop.is_set() and deadline and deadline <= datetime.now(timezone.utc):
            bridge.request_stop(rid, reason='timeout', source='adapter')
        current=store.get_run_status(rid) or {}
        if current.get('stop_requested_reason'):stop.set()
        if context.command_token:
            from . import commands,db
            with db.connect() as conn:
                if not commands._owned(conn,context.command_token):stop.set()
        return stop.is_set()
    try:
        if not store.claim_pending_run(rid):
            return
        attempt = recovery.start_attempt(rid)
        from lea.runctx import run_context
        with run_context(should_stop=preparation_stopped):
            manifest = operations.prepare(op['id'])
        op = operations.get(op['id'])
        operations.fresh(op)
        repo = Path(manifest['repo']).resolve()
        row = store.get_run(rid)
        project = store.get_project(op['project_id'])
        form = store.get_formalization(op['formalization_id'])
        reports = manifest['reports']
        from .reformalization_scope import resolve
        choices = []
        for path, report in reports.items():
            if report:
                try:
                    choices.append((path, resolve(report, form['declaration_name'])))
                except ValueError:
                    pass
        dependency = project_dependencies.get(row['dependency_id'], reconcile=False) if row.get('dependency_id') else None
        if not choices and dependency:
            choices = [(dependency['path'], {'user_name': dependency['declaration_name']})]
        if len(choices) != 1:
            raise ValueError('The requested target has no unambiguous compiler-established source boundary')
        target_path, declaration = choices[0]
        from lea.tools import TOOL_HANDLERS
        allowed = set(TOOL_HANDLERS) | {'safe_verify', 'select_result', 'search_project_library', 'request_project_dependency', 'update_lea_status', 'spawn_subagent'}
        selected_tools = [n for n in (context.config.tools or list(TOOL_HANDLERS)) if n in allowed]
        descriptor = {'path': str(repo / target_path), 'declaration': declaration['user_name'],
                      'target_declaration': declaration['user_name'],
                      'kind': 'defined' if form['kind'] == 'definition' else 'proved'}
        saved_selection = None
        if row.get('resume_checkpoint_id'):
            saved_selection = recovery.continuation_checkpoint(row['resumes_run_id'], row['resume_checkpoint_id'])[0].get('proof_state', {}).get('selection')
        elif row.get('recovery_state') == 'recovering':
            saved_selection = (recovery.load_checkpoint(rid) or ({}, {}))[0].get('proof_state', {}).get('selection')
        if saved_selection:
            _relative(saved_selection.get('path') or descriptor['path'], repo)
            descriptor.update(saved_selection)
        cfg = replace(context.config, selected_result=descriptor,
            prompt_variant='default' if context.autonomous else context.config.prompt_variant,
            mcp_servers={}, http_tools=[], tool_modules=[],
            extra_tools=list(dict.fromkeys([*context.config.extra_tools, 'select_result'])),
            tools=list(dict.fromkeys([*selected_tools, 'select_result'])),
            system_prompt_head=(context.config.system_prompt_head or '') +
                '\nReformalization: work in the supplied isolated project copy. Preserve all unrelated '
                'declarations and comments exactly. Finish the selected target and any established '
                'unfinished helpers it requires, keeping their types. Completed shared mathematics is '
                'protected. Current files override old conversation paths. Use select_result followed '
                'by lean_check if you move the target or produce a differently named disproof. '
                'Only a disproof may select a different declaration. Helper checks do not certify '
                'the target. The host validates and applies the candidate after completion. '
                'You may relocate exact protected declaration commands to suitable modules when needed, '
                'retaining qualified names, types, bodies, visibility and comments, with checked compatibility imports. '
                'Do not edit Lake configuration or dependencies. Keep useful partial work in Lean files '
                'and exploration under .lea/tmp.')
        cfg = bridge._project_library_config(cfg, project, row)
        if not context.autonomous:
            cfg, _ = bridge._with_subagents(cfg)
        from . import skills_catalog
        skills, skill_temp = skills_catalog.materialize_run_skills(project['id'], sid,
            bridge._skill_resolution_task(row, context.task))
        if skills:
            cfg = replace(cfg, skills=skills, skills_root=skill_temp)
        budget = json.loads(row.get('automation_budget_json') or '{}')
        if budget.get('max_cost_usd') is not None:
            cfg = replace(cfg, max_cost_usd=max(0, budget['max_cost_usd'] - budget.get('cost_used', 0)))
        reporting = lea_status_store.context(rid)
        if reporting:
            cfg = replace(cfg, status_reporting=True, status_context=lea_status.seed(rid),
                allow_source_pause=context.allow_source_pause, narrate_tool_steps=False,
                prompt_variant='overleaf_faithful' if context.purpose == 'overleaf_solver' else 'overleaf_continuation',
                extra_tools=list(dict.fromkeys([*cfg.extra_tools, 'update_lea_status'])),
                tools=list(dict.fromkeys([*cfg.tools, 'update_lea_status'])) if cfg.tools else None)
        prior = (recovery.load_safe_conversation(row['resumes_run_id'])
                 if row.get('resumes_run_id') and not row.get('resume_checkpoint_id') else None)
        if prior is None:
            prior = store.latest_transcript_for_session(sid, exclude_run_id=rid) or []
        prior = [m for m in prior if not projects.is_context_message(m) and not bridge._is_formalization_context_message(m)]
        ctx = projects.compose_context_message(project, repo)
        task = context.task + '\n\nCurrent retry workspace: ' + str(repo) + '\nSelected declaration: ' + declaration['user_name'] + '\nCurrent target file: ' + str(repo / target_path)
        retained = operations.retained_context(op)
        if retained:
            task += '\nPrior retained drafts are available as advisory progress. Reuse useful ideas against the current source and baseline; these files carry no current check or approval:\n' + '\n'.join(retained)
        messages = ([ctx] if ctx else []) + [bridge._formalization_context_message(form)] + prior + [{'role': 'user', 'content': task}]
        provider = lambda: recovery.manifest(run=store.get_run(rid), config=cfg, repo=repo)
        recorder = recovery.Recorder(rid, attempt, provider,command_token=context.command_token)
        resume_state = None
        if row.get('recovery_state') == 'recovering':
            saved = recovery.load_checkpoint(rid)
            if not saved or not recovery.compatible_crash_manifest(saved[1], provider(), saved[0]):
                raise ValueError('Saved attempt inputs changed; its drafts are retained')
            resume_state = saved[0]
        if row.get('resume_checkpoint_id'):
            resume_state, changed = recovery.prepare_continuation(row, cfg, repo, lea_status.seed(rid), task)
        def should_stop():
            current = store.get_run(rid) or {}
            if current.get('deadline_at') and datetime.fromisoformat(current['deadline_at']) <= datetime.now(timezone.utc):
                bridge.request_stop(rid, reason='timeout', source='adapter')
            if cfg.max_spend_usd is not None and bridge._persisted_spend_usd() >= cfg.max_spend_usd:
                bridge.request_stop(rid, reason='global_spend_cap', source='adapter')
            return preparation_stopped()
        policy = {**operations.workspace_policy(manifest['workspace']),
                  'canonical_repo': manifest['canonical_repo'], 'attempt_repo': str(repo)}
        # Every prover tool thread copies this ContextVar, including compiler and
        # shell children. Typed host operations install their own canonical policy.
        with execution.scope(policy):
            gen = bridge.run_events(cfg, messages, namespace=manifest['namespace'], session_id=sid,
                working_dir=str(repo), should_stop=should_stop,
                gate=None if context.autonomous or context.approval_mode == 'none' else bridge._make_gate(sid,persisted=bool(context.command_token)),
                resume_state=resume_state, checkpoint_callback=recorder)
            answer = None
            while True:
                try:
                    ev = gen.send(answer)
                except StopIteration:
                    break
                answer = None
                if isinstance(ev, ToolApprovalRequested):
                    answer = bridge._await_decision(rid, sid, ev, events, stop)
                elif isinstance(ev, TurnStarted):
                    turn = ev.turn
                    bridge.emit(events, 'status', {'status': 'running', 'turn': turn, 'artifact_scope': 'attempt'})
                elif isinstance(ev, AssistantTextDelta):
                    narration.append(ev.text)
                    bridge.emit(events, 'assistant_delta', {'text': ev.text})
                elif isinstance(ev, ToolCalled):
                    bridge.emit(events, 'status', {'status': 'tool_call', 'message': 'Running ' + ev.name, 'turn': turn})
                elif isinstance(ev, (FileChanged, CheckResult)):
                    rel = _relative(ev.path, repo)
                    content = ev.content if ev.content is not None else (repo / rel).read_bytes().decode('utf-8')
                    evidence = {'status': ev.status, 'detail': ev.detail, 'execution': ev.execution} if isinstance(ev, CheckResult) else None
                    draft = operations.save_draft(op['id'], rid, rel, content, evidence)
                    bridge.emit(events, 'code_step', draft)
                    if isinstance(ev, CheckResult) and ev.status == 'ok':
                        from .reformalization_helpers import publish
                        with execution.scope(None):
                            result = publish(op['id'], rid, rel)
                        if result:
                            bridge.emit(events, 'project_dependency', result)
                elif isinstance(ev, UsageUpdated):
                    incoming += ev.input_tokens
                    outgoing += ev.output_tokens
                    cost += ev.cost
                    revision += 1
                    usage.add(turn, ev.input_tokens, ev.output_tokens, ev.cost, ev.usage_details)
                    if store.checkpoint_run_usage(rid, incoming, outgoing, cost, revision, ev.usage_details):
                        bridge._advance_spend_cache(ev.cost)
                elif isinstance(ev, SubagentStarted):
                    child, child_run = bridge._start_subagent(ev, parent_session_id=sid,
                        project_id=op['project_id'], turn=turn, cfg=cfg)
                    children[ev.result_id] = {'session': child, 'run_id': child_run,
                        'input': 0, 'output': 0, 'cost': 0.0, 'revision': 0}
                    bridge._child_session_to_result[child['id']] = ev.result_id
                    bridge.emit(events, 'subagent_started', {'child_id': child['id'], 'run_id': child_run,
                        'result_id': ev.result_id, 'title': child['title'], 'subagent_type': ev.subagent_type})
                elif isinstance(ev, SubagentProgress):
                    child = children.get(ev.result_id)
                    if child:
                        if isinstance(ev.event, UsageUpdated):
                            child['input'] += ev.event.input_tokens
                            child['output'] += ev.event.output_tokens
                            child['cost'] += ev.event.cost
                            child['revision'] += 1
                            store.checkpoint_run_usage(child['run_id'], child['input'], child['output'],
                                child['cost'], child['revision'], ev.event.usage_details)
                        if isinstance(ev.event, Finished):
                            store.update_run(child['run_id'], 'running', input_tokens=ev.event.usage.input_tokens,
                                output_tokens=ev.event.usage.output_tokens, cost_usd=ev.event.cost,
                                usage_details=ev.event.usage.usage_details, usage_status='final')
                        payload = bridge._subagent_progress_payload(child['session']['id'], ev.result_id, ev.event)
                        if payload:
                            bridge.emit(events, 'subagent_progress', payload)
                elif isinstance(ev, SubagentFinished):
                    child = children.pop(ev.result_id, None)
                    if ev.content is not None:
                        operations.save_draft(op['id'], child['run_id'] if child else rid,
                            'children/' + ev.result_id + '/' + Path(ev.candidate_path or 'Candidate.lean').name,
                            ev.content, {'status': ev.check_status, 'detail': ev.check_detail, 'execution': ev.execution})
                    if child:
                        store.set_run_transcript(child['run_id'], ev.transcript)
                        store.add_message(child['session']['id'], 'assistant', ev.summary, run_id=child['run_id'])
                        store.update_run(child['run_id'], 'needs_review', result_kind='needs_review', final_text=ev.summary, usage_status='final')
                        bridge._child_session_to_result.pop(child['session']['id'], None)
                        bridge.emit(events, 'subagent_finished', {'child_id': child['session']['id'], 'run_id': child['run_id'],
                            'result_id': ev.result_id, 'check_status': ev.check_status, 'artifact_scope': 'attempt', 'summary': ev.summary})
                elif isinstance(ev, LeaStatusUpdateRequested):
                    _capture(op, rid, repo)
                    try:
                        from lea.status_reporting import attention
                        update, payload = lea_status.publish(rid, ev.invocation_key, ev.payload)
                        answer = LeaStatusUpdateAck(True, update['id'], update['sequence'], update['assessment'],
                            stop_reason='source_obstruction' if context.allow_source_pause
                            and attention(update['assessment']) == 'needs_author_input' else None)
                        bridge.emit(events, 'lea_status_updated', payload)
                    except ValueError as exc:
                        answer = LeaStatusUpdateAck(False, error=str(exc))
                elif isinstance(ev, ProjectLibraryRequested):
                    try:
                        with execution.scope(operations.canonical_policy(manifest['canonical_root'], manifest['canonical_repo'])):
                            result = project_dependencies.tool_request(rid, ev.invocation_key, ev.tool, ev.arguments)
                        answer = ProjectLibraryAck(result=result)
                        bridge.emit(events, 'project_dependency', result)
                    except (ValueError, OSError) as exc:
                        answer = ProjectLibraryAck(error=str(exc))
                elif isinstance(ev, DependencyReadinessRequested):
                    from .reformalization_helpers import readiness
                    answer = DependencyReadinessAck(**readiness(op['id'], rid, ev.path))
                elif isinstance(ev, Diagnostic):
                    bridge.diagnose(events, sid, rid, ev.severity, ev.code, ev.message, **ev.context)
                elif isinstance(ev, Finished):
                    _capture(op, rid, repo)
                    store.set_run_transcript(rid, ev.transcript.get('messages', []))
                    kind = ev.result_kind or 'needs_review'
                    detail = ev.result_detail or ev.text
                    if ev.reason == 'completed' and not should_stop():
                        chosen = ev.selected_result or {}
                        selection = {'path': _relative(ev.candidate_path or cfg.selected_result['path'], repo),
                            'declaration': chosen.get('user_name') or cfg.selected_result['declaration'],
                            'kind': cfg.selected_result['kind'], 'outcome': cfg.selected_result['kind']}
                        with execution.scope(None):
                            assessment = assess(op['id'], selection)
                        selection = assessment['selection']
                        if kind in {'proved', 'defined', 'disproved'}:
                            if kind != selection['kind']:
                                kind = 'needs_review'
                                detail = 'The selected declaration and reported outcome require correspondence review.'
                        if kind in {'proved', 'defined', 'disproved'}:
                            if should_stop():raise ValueError('Solver was cancelled before publication; the draft is retained')
                            with execution.scope(None):
                                receipt = apply(op['id'], selection, kind, assessment=assessment, helper=dependency,command_token=context.command_token)
                            status = kind
                            detail = kind.upper()
                            bridge.emit(events, 'reformalization_applied', receipt)
                        else:
                            status = kind = 'needs_review'
                            for rel in assessment['checks']:
                                if rel == selection['path'] or rel in assessment['changes']:
                                    operations.save_draft(op['id'], rid, rel, assessment['after'][rel], assessment['checks'][rel])
                            operations.update(op['id'], 'review', result={'outcome': kind, 'detail': detail,
                                'selection': selection, 'drafts': [{'path': d['path'], 'blob_id': d['blob_id']} for d in operations.drafts(op['id'])]})
                    else:
                        status, kind, reason, recoverable = 'cancelled', 'interrupted', ev.reason, True
                        if ev.reason=='completed':reason='interrupted'
                        if reason == 'interrupted':
                            reason = (store.get_run(rid) or {}).get('stop_requested_reason') or 'user_stop'
                        operations.update(op['id'], ('worker' if op['request'].get('_foreground_owner') else 'active') if reason == 'dependency_wait' else 'paused',
                            result={'outcome': kind, 'detail': detail, 'stop_reason': reason})
                    incoming, outgoing, cost = ev.usage.input_tokens, ev.usage.output_tokens, ev.cost
                    committed=store.update_run(rid, status, final_text=ev.text, input_tokens=incoming,
                        output_tokens=outgoing, cost_usd=cost, usage_status='final', usage_details=ev.usage.usage_details,
                        result_kind=kind, result_detail=detail, stop_reason=reason, recoverable=recoverable,command_token=context.command_token)
                    if committed:
                        status,kind,detail,reason,recoverable=(committed[k] for k in ('status','result_kind','result_detail','stop_reason','recoverable'))
                    break
    except Exception as exc:
        detail = bridge._public_error_detail(exc)
        if operations.get(op['id'])['state'] != 'settled':
            operations.update(op['id'], 'retained', result={'outcome': 'conflict', 'detail': detail})
            store.update_run(rid, 'failed', result_kind='failed', result_detail=detail)
        else:
            accepted = store.get_run(rid)
            status, kind = accepted['status'], accepted['result_kind']
        bridge.emit(events, 'run_error', {'message': detail})
    finally:
        if repo:
            _capture(op, rid, repo)
        store.replace_run_usage_breakdown(rid, usage.rows())
        from . import skills_catalog
        skills_catalog.cleanup(skill_temp)
        for child in children.values():
            store.update_run(child['run_id'], 'cancelled', result_kind='interrupted')
            bridge._child_session_to_result.pop(child['session']['id'], None)
        if narration:
            store.add_message(sid, 'assistant', ''.join(narration), run_id=rid)
        if attempt:
            recovery.finish_attempt(rid, attempt, status)
        settled = store.get_run(rid) or {}
        budget = json.loads(settled.get('automation_budget_json') or '{}')
        if budget:
            budget['remaining_seconds'] = max(0, budget.get('remaining_seconds', 900) - (time.monotonic() - started))
            budget['cost_used'] = budget.get('cost_used', 0) + float(settled.get('cost_usd') or 0)
            budget['settled_run_id'] = rid
            from .db import write
            with write() as conn:
                conn.execute('update runs set automation_budget_json=? where id=?', (json.dumps(budget), rid))
        bridge._stop_events.pop(rid, None)
        bridge._pending_approvals.pop(rid, None)
        project_dependencies.settle_run(rid)
        bridge.emit(events, 'done', {**{k: settled.get(k) for k in ('status', 'result_kind', 'result_detail', 'stop_reason', 'recoverable', 'attempt_id')},
            'reformalization_id': op['id'], 'artifact_scope': 'canonical' if operations.get(op['id'])['state'] == 'settled' else 'attempt'})
        runbroker.drop(rid)
