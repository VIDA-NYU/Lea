"""Durable application, reconciliation, checking, and recovery for GitHub imports."""

from __future__ import annotations

import json
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from lea.interface import check as interface_check

from . import formalizations as formalizations_service
from . import store
from .artifacts import (
    classify_lean_artifact,
    declaration_contains_sorry,
    declaration_present,
    scan_lean_declarations,
)
from .config import github_token
from .db import ROOT
from .github_project_import import (
    ImportPlanningError,
    TaggedTarget,
    formalization_kind_compatible,
    inventory_source,
    plan_import,
    preview_registry,
    temporary_clone_path,
)
from .github_source import clone_repository, parse_github_repository_url
from .gitstore import GitStore
from .projects import project_repo_dir


STAGING_ROOT = ROOT / "data" / "github-imports"
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="github-import-check")
_enqueued: set[str] = set()
_enqueued_lock = threading.Lock()


def _source_label(source_url: str) -> str:
    path = urlparse(source_url).path.strip("/")
    return path or "GitHub repository"


def preview_import(
    *,
    project: dict,
    proofs_root: Path,
    repository_url: str,
    targets: list[TaggedTarget] | None = None,
) -> dict:
    source = parse_github_repository_url(repository_url)
    clone_path = temporary_clone_path()
    try:
        source_sha = clone_repository(
            source.clone_url,
            clone_path,
            token=github_token(),
        )
        inventory = inventory_source(clone_path)
        target_repo = project_repo_dir(project, proofs_root)
        current_formalizations = formalizations_service.for_project(project["id"])
        target_list = targets or []
        plan = plan_import(
            inventory,
            target_repo,
            project["namespace"],
            current_formalizations,
            target_list,
        )
        preview = preview_registry.add(
            project_id=project["id"],
            project_namespace=project["namespace"],
            source_url=source.source_url,
            source_ref=None,
            source_commit_sha=source_sha,
            clone_path=clone_path,
            inventory=inventory,
            plan=plan,
            targets=target_list,
        )
    except Exception:
        shutil.rmtree(clone_path.parent, ignore_errors=True)
        raise
    return {
        "preview_id": preview.id,
        "expires_in_seconds": preview_registry.ttl_seconds,
        "source": {
            "url": source.source_url,
            "owner": source.owner,
            "repository": source.name,
            "ref": None,
            "commit_sha": source_sha,
        },
        "project": {
            "id": project["id"],
            "slug": project["slug"],
            "namespace": project["namespace"],
        },
        "plan": plan.public_dict(),
    }


def _staging_source(import_id: str) -> Path:
    return STAGING_ROOT / import_id / "source"


def _persist_planned_files(import_id: str, plan) -> None:
    for item in plan.files:
        store.upsert_github_import_file(
            import_id=import_id,
            source_path=item.source_path,
            destination_path=item.destination_path,
            disposition=item.disposition,
            reason=item.reason,
            content_sha256=item.content_sha256,
        )


def _upsert_target(project_id: str, target: TaggedTarget) -> tuple[dict | None, str | None]:
    by_origin = store.find_formalization_by_origin(project_id, "overleaf", target.origin_key)
    by_declaration = store.find_formalization_by_declaration(
        project_id=project_id,
        loose_session_id=None,
        declaration_name=target.declaration_name,
    )
    if by_origin and by_declaration and by_origin["id"] != by_declaration["id"]:
        return None, "Origin key and declaration name identify different formalizations."
    row = by_origin or by_declaration
    if row:
        try:
            updated = store.update_formalization(
                row["id"],
                display_title=target.display_title,
                declaration_name=target.declaration_name,
                statement=target.statement,
                kind=target.kind,
                source_hash=target.source_hash,
            )
        except Exception as exc:
            return None, str(exc)
        return updated, None
    try:
        return store.create_formalization(
            project_id=project_id,
            loose_session_id=None,
            display_title=target.display_title,
            declaration_name=target.declaration_name,
            kind=target.kind,
            statement=target.statement,
            origin="overleaf",
            origin_key=target.origin_key,
            source_hash=target.source_hash,
        ), None
    except Exception as exc:
        return None, str(exc)


def sync_targets(project: dict, targets: list[TaggedTarget], proofs_root: Path) -> dict:
    results = []
    for target in targets:
        row, conflict = _upsert_target(project["id"], target)
        adoption = None
        if row:
            adoption = try_adopt_imported_declaration(project, row, proofs_root)
        results.append(
            {
                "origin_key": target.origin_key,
                "declaration_name": target.declaration_name,
                "formalization_id": row.get("id") if row else None,
                "conflict": conflict,
                "adoption": adoption,
            }
        )
    return {"project_id": project["id"], "targets": results}


def confirm_import(
    *,
    project: dict,
    proofs_root: Path,
    preview_id: str,
) -> dict:
    # Keep the preview reusable when a temporary project lock rejects confirmation.
    # A successful confirmation (including an idempotent replay) consumes it below.
    preview = preview_registry.get(preview_id)
    if preview.project_id != project["id"] or preview.project_namespace != project["namespace"]:
        raise ImportPlanningError("import_preview_mismatch", "Preview belongs to another project state.")
    if preview.plan.blocking_error:
        raise ImportPlanningError(
            preview.plan.blocking_error["code"], preview.plan.blocking_error["message"]
        )
    if store.project_has_active_run(project["id"]):
        raise ImportPlanningError("project_busy", "A Lea run is currently writing this project.")
    if store.project_has_active_import(project["id"]):
        raise ImportPlanningError("project_busy", "Another GitHub import is active for this project.")

    # Re-analyzing and confirming the same commit after a successful import is a
    # no-op when every importable file is already byte-identical. Reuse the prior
    # provenance record instead of manufacturing another session and code steps.
    repeatable = not any(item.disposition == "add" for item in preview.plan.files)
    if repeatable:
        previous = next(
            (
                row
                for row in store.list_project_github_imports(project["id"])
                if row["source_url"] == preview.source_url
                and row["source_commit_sha"] == preview.source_commit_sha
                and row["destination_namespace"] == project["namespace"]
                and row["status"] in {"complete", "complete_with_issues"}
            ),
            None,
        )
        if previous:
            preview_registry.get(preview_id, consume=True)
            try:
                if preview.targets:
                    sync_targets(project, preview.targets, proofs_root)
                progress = store.github_import_progress(previous["id"]) or previous
                return {**progress, "reused": True}
            finally:
                preview_registry.discard(preview_id)

    preview = preview_registry.get(preview_id, consume=True)

    import_id = str(uuid4())
    staging = _staging_source(import_id)
    staging.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(preview.clone_path), str(staging))
    preview_registry.discard(preview_id)
    imported = store.create_github_import(
        import_id=import_id,
        project_id=project["id"],
        source_url=preview.source_url,
        source_ref=preview.source_ref,
        source_commit_sha=preview.source_commit_sha,
        source_namespace=preview.plan.source_namespace,
        destination_namespace=project["namespace"],
        destination_snapshot=preview.plan.destination_snapshot,
    )
    _persist_planned_files(import_id, preview.plan)
    try:
        _apply_import(imported, project, proofs_root, preview.inventory, preview.targets)
    except Exception as exc:
        store.set_github_import_status(import_id, "failed", error_detail=str(exc))
        shutil.rmtree(staging.parent, ignore_errors=True)
        raise
    # Applying and indexing are durable now; checking reads only destination
    # files and DB rows, so the untrusted source clone is no longer needed.
    shutil.rmtree(staging.parent, ignore_errors=True)
    enqueue_import(import_id, proofs_root)
    return store.github_import_progress(import_id) or imported


def _exclusive_add(path: Path, content: str, expected_hash: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        try:
            current_hash = __import__("hashlib").sha256(path.read_bytes()).hexdigest()
        except OSError:
            return "path_conflict"
        return "already_present" if current_hash == expected_hash else "path_conflict"
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
        handle.write(content)
    return "add"


def _apply_import(
    imported: dict,
    project: dict,
    proofs_root: Path,
    inventory,
    targets: list[TaggedTarget],
) -> None:
    import_id = imported["id"]
    if store.project_has_active_run(project["id"]):
        raise ImportPlanningError("project_busy", "A Lea run is currently writing this project.")
    if store.project_has_active_import(project["id"], exclude_import_id=import_id):
        raise ImportPlanningError("project_busy", "Another GitHub import is active for this project.")
    repo = project_repo_dir(project, proofs_root)
    current_formalizations = formalizations_service.for_project(project["id"])
    plan = plan_import(
        inventory,
        repo,
        project["namespace"],
        current_formalizations,
        targets,
    )
    added: list[str] = []
    for item in plan.files:
        if item.disposition == "add" and item.destination_path and item.rewritten_content is not None:
            final_disposition = _exclusive_add(
                repo / item.destination_path,
                item.rewritten_content,
                item.content_sha256 or "",
            )
            if final_disposition == "add":
                added.append(item.destination_path)
            elif final_disposition == "already_present":
                item.reason = "Destination became present with identical content during confirmation."
            else:
                item.reason = "Destination became occupied with different content during confirmation."
            item.disposition = final_disposition
        store.upsert_github_import_file(
            import_id=import_id,
            source_path=item.source_path,
            destination_path=item.destination_path,
            disposition=item.disposition,
            reason=item.reason,
            content_sha256=item.content_sha256,
        )

    commit_sha = None
    if added:
        commit_sha = GitStore(proofs_root).commit_all(
            repo,
            f"import {len(added)} Lean files from {_source_label(imported['source_url'])}"
            f"@{imported['source_commit_sha'][:8]}",
            paths=added,
        )

    target_rows: dict[str, dict] = {}
    target_conflicts: dict[str, str] = {}
    for target in targets:
        row, conflict = _upsert_target(project["id"], target)
        if row:
            target_rows[target.origin_key] = row
        if conflict:
            target_conflicts[target.origin_key] = conflict

    session_id = imported.get("session_id")
    if not session_id:
        session = store.create_session(
            f"GitHub import — {_source_label(imported['source_url'])} @ "
            f"{imported['source_commit_sha'][:8]}",
            project_id=project["id"],
            origin="github_import",
            origin_url=imported["source_url"],
        )
        session_id = session["id"]
    store.set_github_import_status(
        import_id,
        "applying",
        session_id=session_id,
        commit_sha=commit_sha,
        error_detail=json.dumps({"target_conflicts": target_conflicts}) if target_conflicts else None,
    )

    persisted_files = {row["source_path"]: row for row in store.list_github_import_files(import_id)}
    for item in plan.files:
        if item.disposition not in {"add", "already_present"} or not item.destination_path:
            continue
        absolute = repo / item.destination_path
        try:
            content = absolute.read_text(encoding="utf-8")
        except OSError:
            continue
        file_row = persisted_files.get(item.source_path) or {}
        code_step_id = file_row.get("code_step_id")
        if not code_step_id:
            step = store.add_code_step(
                session_id,
                None,
                item.destination_path,
                content=content,
                author="environment",
                summary=f"Imported from {imported['source_url']}@{imported['source_commit_sha'][:8]}",
                provenance={
                    "github_import_id": import_id,
                    "source_url": imported["source_url"],
                    "source_commit_sha": imported["source_commit_sha"],
                },
            )
            code_step_id = int(step["id"])
        store.upsert_github_import_file(
            import_id=import_id,
            source_path=item.source_path,
            destination_path=item.destination_path,
            disposition=item.disposition,
            reason=item.reason,
            content_sha256=item.content_sha256,
            code_step_id=code_step_id,
            check_status="pending",
        )

        module_name = item.module_name or ""
        for declaration in scan_lean_declarations(content):
            match = next(
                (
                    planned.match
                    for planned in item.declarations
                    if planned.full_name == declaration.full_name
                ),
                None,
            )
            formalization = None
            if match and match.formalization_id:
                formalization = store.get_formalization(match.formalization_id)
            elif match and match.origin_key:
                formalization = target_rows.get(match.origin_key)
            if formalization is None:
                candidates = [
                    row for row in store.list_raw_project_formalizations(project["id"])
                    if row.get("declaration_name") in {declaration.full_name, declaration.short_name}
                ]
                if len(candidates) == 1 and formalization_kind_compatible(
                    str(candidates[0].get("kind") or "other"), declaration.kind
                ):
                    formalization = candidates[0]
            declaration_row = store.upsert_github_import_declaration(
                import_id=import_id,
                project_id=project["id"],
                destination_path=item.destination_path,
                declaration_name=declaration.short_name,
                full_name=declaration.full_name,
                kind=declaration.kind,
                module_name=module_name,
                formalization_id=formalization.get("id") if formalization else None,
                source_hash_at_match=formalization.get("source_hash") if formalization else None,
            )
            if not formalization:
                continue
            primary = next(
                (
                    row for row in store.list_formalization_files(formalization["id"])
                    if row["role"] == "primary"
                ),
                None,
            )
            if primary and primary["path"] != item.destination_path:
                continue
            store.link_session_formalization(session_id, formalization["id"])
            store.link_formalization_file(formalization["id"], item.destination_path, "primary")
            artifact_kind = "definition" if declaration.kind == "definition" else "proof"
            store.upsert_artifact(
                project_id=project["id"],
                session_id=session_id,
                run_id=None,
                declaration_name=str(formalization.get("declaration_name") or declaration.full_name),
                kind=artifact_kind,
                path=item.destination_path,
                module_name=module_name,
                formalization_id=formalization["id"],
                source_hash=formalization.get("source_hash"),
            )
            store.bind_github_import_declaration(
                declaration_row["id"], formalization["id"], formalization.get("source_hash")
            )
    store.set_github_import_status(import_id, "checking", session_id=session_id, commit_sha=commit_sha)


def enqueue_import(import_id: str, proofs_root: Path) -> None:
    with _enqueued_lock:
        if import_id in _enqueued:
            return
        _enqueued.add(import_id)
    _executor.submit(_check_import, import_id, Path(proofs_root))


def _check_import(import_id: str, proofs_root: Path) -> None:
    try:
        imported = store.get_github_import(import_id)
        if not imported or imported["status"] not in {"applying", "checking"}:
            return
        project = store.get_project(imported["project_id"])
        if not project:
            store.set_github_import_status(import_id, "failed", error_detail="Target project was deleted.")
            return
        repo = project_repo_dir(project, proofs_root)
        seen: set[str] = set()
        for row in store.list_github_import_files(import_id):
            path = row.get("destination_path")
            if row["disposition"] not in {"add", "already_present"} or not path or path in seen:
                continue
            seen.add(path)
            if row.get("check_status") in {"ok", "error"}:
                continue
            absolute = repo / path
            try:
                result = interface_check(str(absolute))
                status = "ok" if result.status == "ok" else "error"
                detail = result.detail
            except Exception as exc:
                status, detail = "error", str(exc)
            if row.get("code_step_id"):
                content = absolute.read_text(encoding="utf-8", errors="replace") if absolute.is_file() else ""
                store.set_code_step_check(
                    str(row["code_step_id"]),
                    status,
                    detail,
                    classify_lean_artifact(content),
                )
            store.set_github_import_file_check(import_id, row["source_path"], status, detail)

        progress = store.github_import_progress(import_id)
        issues = any(
            row.get("check_status") != "ok"
            for row in (progress or {}).get("files", [])
            if row.get("disposition") in {"add", "already_present"}
        )
        for declaration in (progress or {}).get("declarations", []):
            if not declaration.get("formalization_id"):
                continue
            absolute = repo / declaration["destination_path"]
            try:
                content = absolute.read_text(encoding="utf-8")
            except OSError:
                issues = True
                continue
            if declaration_contains_sorry(content, declaration["full_name"]):
                issues = True
        store.set_github_import_status(
            import_id,
            "complete_with_issues" if issues else "complete",
        )
        shutil.rmtree(_staging_source(import_id).parent, ignore_errors=True)
    finally:
        with _enqueued_lock:
            _enqueued.discard(import_id)


def recover_github_imports_at_startup(proofs_root: Path | None) -> None:
    if proofs_root is None:
        return
    for imported in store.list_recoverable_github_imports():
        if imported["status"] == "applying":
            staging = _staging_source(imported["id"])
            project = store.get_project(imported["project_id"])
            if staging.is_dir() and project:
                try:
                    inventory = inventory_source(staging)
                    _apply_import(imported, project, proofs_root, inventory, [])
                except Exception as exc:
                    store.set_github_import_status(
                        imported["id"], "failed", error_detail=f"Recovery failed: {exc}"
                    )
                    continue
            else:
                store.set_github_import_status(
                    imported["id"],
                    "complete_with_issues",
                    error_detail="Import was interrupted before durable staging completed.",
                )
                continue
        enqueue_import(imported["id"], proofs_root)


def try_adopt_imported_declaration(
    project: dict,
    formalization: dict,
    proofs_root: Path,
) -> dict:
    name = str(formalization.get("declaration_name") or "")
    if not name:
        return {"adopted": False, "reason": "Formalization has no declaration name."}
    candidates = store.find_unbound_imported_declarations(project["id"], name)
    if len(candidates) != 1:
        return {
            "adopted": False,
            "reason": "No unambiguous imported declaration is available.",
        }
    candidate = candidates[0]
    if not candidate.get("code_step_id"):
        return {"adopted": False, "reason": "Imported declaration has no local check step."}
    if not formalization_kind_compatible(
        str(formalization.get("kind") or "other"), str(candidate.get("kind") or "other")
    ):
        return {
            "adopted": False,
            "reason": "Imported declaration kind is incompatible with the formalization.",
        }
    repo = project_repo_dir(project, proofs_root)
    path = str(candidate["destination_path"])
    absolute = repo / path
    try:
        content = absolute.read_text(encoding="utf-8")
    except OSError:
        return {"adopted": False, "reason": "Imported file is no longer present."}
    if not declaration_present(content, name):
        return {"adopted": False, "reason": "Imported declaration is no longer present."}
    primary = next(
        (row for row in store.list_formalization_files(formalization["id"]) if row["role"] == "primary"),
        None,
    )
    if primary and primary["path"] != path:
        return {"adopted": False, "reason": "Formalization already has another primary file."}
    session_id = candidate.get("session_id")
    if not session_id:
        return {"adopted": False, "reason": "Import provenance session is unavailable."}
    store.link_session_formalization(session_id, formalization["id"])
    store.link_formalization_file(formalization["id"], path, "primary")
    artifact_kind = "definition" if candidate["kind"] == "definition" else "proof"
    store.upsert_artifact(
        project_id=project["id"],
        session_id=session_id,
        run_id=None,
        declaration_name=name,
        kind=artifact_kind,
        path=path,
        module_name=candidate["module_name"],
        formalization_id=formalization["id"],
        source_hash=formalization.get("source_hash"),
    )
    store.bind_github_import_declaration(
        candidate["id"], formalization["id"], formalization.get("source_hash")
    )
    return {"adopted": True, "path": path, "import_id": candidate["import_id"]}
