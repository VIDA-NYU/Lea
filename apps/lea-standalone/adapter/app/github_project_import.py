"""Plan and orchestrate additive Lean-file imports from GitHub repositories."""

from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
import threading
import time
import unicodedata
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from uuid import uuid4

from .artifacts import LeanDeclaration, scan_lean_declarations, scrub_lean_source
from .github_source import tracked_lean_files
from .projects import rewrite_namespace_text


EXCLUDED_DIRECTORY_NAMES = {
    ".git", ".lake", "build", "dist", "generated", "lake-packages",
    "node_modules", "vendor", "vendors", "third_party", "third-party",
}
LFS_HEADER = "version https://git-lfs.github.com/spec/v1"
PROJECT_NAMESPACE_RE = re.compile(r"(?<![A-Za-z0-9_])(Lea\.[A-Za-z][A-Za-z0-9_]*)")
MODULE_SEGMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_']*$")


class ImportPlanningError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ImportLimits:
    max_files: int = 1000
    max_file_bytes: int = 1024 * 1024
    max_total_bytes: int = 100 * 1024 * 1024


@dataclass(frozen=True)
class SourceLeanFile:
    source_path: str
    git_mode: str
    content: str | None
    size_bytes: int
    excluded_reason: str | None = None


@dataclass(frozen=True)
class SourceInventory:
    files: list[SourceLeanFile]
    total_bytes: int


@dataclass(frozen=True)
class NamespaceInference:
    namespace: str | None
    coherent: bool
    candidates: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaggedTarget:
    origin_key: str
    label: str
    declaration_name: str
    kind: str
    display_title: str
    statement: str | None = None
    source_hash: str | None = None


@dataclass(frozen=True)
class DeclarationMatch:
    declaration_name: str
    formalization_id: str | None
    origin_key: str | None
    display_title: str
    source_hash: str | None


@dataclass(frozen=True)
class PlannedDeclaration:
    short_name: str
    full_name: str
    keyword: str
    kind: str
    start_line: int
    end_line: int
    match: DeclarationMatch | None = None


@dataclass
class PlannedFile:
    source_path: str
    destination_path: str | None
    disposition: str
    reason: str
    content_sha256: str | None
    module_name: str | None
    declarations: list[PlannedDeclaration] = field(default_factory=list)
    rewritten_content: str | None = field(default=None, repr=False)


@dataclass
class ImportPlan:
    source_namespace: str | None
    destination_namespace: str
    destination_snapshot: str
    files: list[PlannedFile]
    blocking_error: dict[str, str] | None = None

    def public_dict(self) -> dict:
        result = asdict(self)
        for item in result["files"]:
            item.pop("rewritten_content", None)
        result["counts"] = dict(Counter(item.disposition for item in self.files))
        result["matched_declarations"] = sum(
            1
            for item in self.files
            for declaration in item.declarations
            if declaration.match is not None
        )
        result["reusable_declarations"] = sum(
            1
            for item in self.files
            for declaration in item.declarations
            if declaration.match is None
            and item.disposition in {"add", "already_present"}
        )
        return result


def _safe_source_path(raw: str) -> PurePosixPath | None:
    path = PurePosixPath(raw)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path


def _excluded_directory(path: PurePosixPath) -> str | None:
    for part in path.parts[:-1]:
        if part.casefold() in EXCLUDED_DIRECTORY_NAMES:
            return part
    return None


def inventory_source(clone: Path, limits: ImportLimits | None = None) -> SourceInventory:
    """Inventory tracked Lean files without following unsafe Git entries."""

    guard = limits or ImportLimits()
    tracked = tracked_lean_files(clone)
    if len(tracked) > guard.max_files:
        raise ImportPlanningError(
            "repository_file_limit",
            f"Repository has {len(tracked)} tracked Lean files; the limit is {guard.max_files}.",
        )
    files: list[SourceLeanFile] = []
    total = 0
    for item in tracked:
        path = _safe_source_path(item.path)
        reason: str | None = None
        content: str | None = None
        size = 0
        if path is None:
            reason = "Unsafe or escaping repository path."
        elif item.mode == "120000":
            reason = "Git symlinks are not imported."
        elif item.mode not in {"100644", "100755"}:
            reason = f"Unsupported Git file mode {item.mode}."
        elif (excluded := _excluded_directory(path)) is not None:
            reason = f"Files beneath {excluded}/ are excluded."
        elif any((clone / Path(*path.parts[:index]) / ".git").exists() for index in range(1, len(path.parts))):
            reason = "Files inside nested repositories are excluded."
        else:
            absolute = clone / Path(*path.parts)
            try:
                raw = absolute.read_bytes()
            except OSError:
                reason = "Tracked file could not be read."
            else:
                size = len(raw)
                if size > guard.max_file_bytes:
                    reason = f"File exceeds the {guard.max_file_bytes}-byte limit."
                elif raw.startswith(LFS_HEADER.encode("ascii")):
                    reason = "Git LFS pointer files are excluded."
                else:
                    try:
                        content = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        reason = "Lean source is not valid UTF-8."
        total += size if content is not None else 0
        files.append(
            SourceLeanFile(
                source_path=item.path,
                git_mode=item.mode,
                content=content,
                size_bytes=size,
                excluded_reason=reason,
            )
        )
    if total > guard.max_total_bytes:
        raise ImportPlanningError(
            "repository_size_limit",
            f"Lean source totals {total} bytes; the limit is {guard.max_total_bytes}.",
        )
    return SourceInventory(files=files, total_bytes=total)


def infer_source_namespace(files: list[SourceLeanFile]) -> NamespaceInference:
    candidates: set[str] = set()
    for item in files:
        if item.content is None:
            continue
        for match in PROJECT_NAMESPACE_RE.finditer(scrub_lean_source(item.content)):
            candidates.add(match.group(1))
    ordered = tuple(sorted(candidates))
    if len(ordered) > 1:
        return NamespaceInference(namespace=None, coherent=False, candidates=ordered)
    return NamespaceInference(
        namespace=ordered[0] if ordered else None,
        coherent=True,
        candidates=ordered,
    )


def destination_snapshot(repository: Path, namespace: str) -> str:
    entries: list[str] = [namespace]
    if repository.is_dir():
        for path in sorted(repository.rglob("*.lean")):
            if any(part in {".git", ".lake", ".lea"} for part in path.parts):
                continue
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                continue
            entries.append(f"{path.relative_to(repository).as_posix()}:{digest}")
    return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()


def _module_name(namespace: str, path: str) -> str:
    relative = PurePosixPath(path).with_suffix("")
    return ".".join((namespace, *relative.parts))


def _has_supported_module_path(path: PurePosixPath) -> bool:
    parts = [*path.parts[:-1], path.stem]
    return bool(parts) and all(MODULE_SEGMENT_RE.fullmatch(part) for part in parts)


def _path_collision_key(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def formalization_kind_compatible(formalization_kind: str, declaration_kind: str) -> bool:
    kind = str(formalization_kind or "other").lower()
    incoming = str(declaration_kind or "other").lower()
    if kind == "definition":
        return incoming == "definition"
    if kind in {"theorem", "lemma", "counterexample", "disproof"}:
        return incoming in {"theorem", "lemma"}
    return False


def _kind_compatible(formalization_kind: str, declaration: LeanDeclaration) -> bool:
    return formalization_kind_compatible(formalization_kind, declaration.kind)


def _target_match(
    declaration: LeanDeclaration,
    formalizations: list[dict],
    tagged_targets: list[TaggedTarget],
) -> tuple[DeclarationMatch | None, str | None]:
    names = {declaration.short_name, declaration.full_name}
    rows = [row for row in formalizations if row.get("declaration_name") in names]
    if len(rows) > 1:
        return None, "Declaration matches more than one existing formalization."
    if rows:
        row = rows[0]
        if not _kind_compatible(str(row.get("kind") or "other"), declaration):
            return None, "Declaration kind is incompatible with the existing formalization."
        return DeclarationMatch(
            declaration_name=str(row.get("declaration_name") or declaration.full_name),
            formalization_id=str(row["id"]),
            origin_key=row.get("origin_key"),
            display_title=str(row.get("display_title") or declaration.short_name),
            source_hash=row.get("source_hash"),
        ), None
    targets = [target for target in tagged_targets if target.declaration_name in names]
    if len(targets) > 1:
        return None, "Declaration matches more than one tagged target."
    if targets:
        target = targets[0]
        if not _kind_compatible(target.kind, declaration):
            return None, "Declaration kind is incompatible with the tagged formalization."
        return DeclarationMatch(
            declaration_name=target.declaration_name,
            formalization_id=None,
            origin_key=target.origin_key,
            display_title=target.display_title,
            source_hash=target.source_hash,
        ), None
    return None, None


def _existing_declarations(repository: Path) -> tuple[dict[str, set[str]], dict[str, bytes]]:
    names: dict[str, set[str]] = defaultdict(set)
    contents: dict[str, bytes] = {}
    if not repository.is_dir():
        return names, contents
    for path in sorted(repository.rglob("*.lean")):
        if any(part in {".git", ".lake", ".lea"} for part in path.parts):
            continue
        rel = path.relative_to(repository).as_posix()
        try:
            raw = path.read_bytes()
            text = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        contents[rel] = raw
        for declaration in scan_lean_declarations(text):
            names[declaration.full_name].add(rel)
            names[declaration.short_name].add(rel)
    return names, contents


def plan_import(
    source: SourceInventory,
    destination_repo: Path,
    destination_namespace: str,
    formalizations: list[dict] | None = None,
    tagged_targets: list[TaggedTarget] | None = None,
) -> ImportPlan:
    """Build a side-effect-free, per-file additive import plan."""

    formalization_rows = formalizations or []
    targets = tagged_targets or []
    inference = infer_source_namespace(source.files)
    existing_names, existing_contents = _existing_declarations(destination_repo)
    existing_casefold = {_path_collision_key(path): path for path in existing_contents}
    planned: list[PlannedFile] = []

    for source_file in source.files:
        if source_file.excluded_reason or source_file.content is None:
            planned.append(
                PlannedFile(
                    source_path=source_file.source_path,
                    destination_path=None,
                    disposition="excluded",
                    reason=source_file.excluded_reason or "File is excluded.",
                    content_sha256=None,
                    module_name=None,
                )
            )
            continue
        source_path = _safe_source_path(source_file.source_path)
        if source_path is None or not inference.coherent or not _has_supported_module_path(source_path):
            planned.append(
                PlannedFile(
                    source_path=source_file.source_path,
                    destination_path=source_file.source_path if source_path else None,
                    disposition="unsupported_module_layout",
                    reason=(
                        "Source files use multiple Lea project namespaces: "
                        + ", ".join(inference.candidates)
                        if not inference.coherent
                        else "Source path cannot be mapped to a valid Lean module."
                    ),
                    content_sha256=None,
                    module_name=None,
                )
            )
            continue
        destination_path = source_path.as_posix()
        content = source_file.content
        if inference.namespace and inference.namespace != destination_namespace:
            content = rewrite_namespace_text(content, inference.namespace, destination_namespace)
        raw = content.encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        declarations = scan_lean_declarations(content)
        planned_declarations: list[PlannedDeclaration] = []
        match_problem: str | None = None
        primary_problem: str | None = None
        for declaration in declarations:
            match, problem = _target_match(declaration, formalization_rows, targets)
            if problem:
                match_problem = problem
            if match and match.formalization_id:
                row = next(
                    item for item in formalization_rows if str(item["id"]) == match.formalization_id
                )
                primary = row.get("primary_path") or next(
                    (
                        item.get("path")
                        for item in row.get("files", [])
                        if item.get("role") == "primary"
                    ),
                    None,
                )
                if primary and primary != destination_path:
                    primary_problem = (
                        f"Formalization {match.display_title} already uses {primary}."
                    )
            planned_declarations.append(
                PlannedDeclaration(
                    short_name=declaration.short_name,
                    full_name=declaration.full_name,
                    keyword=declaration.keyword,
                    kind=declaration.kind,
                    start_line=declaration.start_line,
                    end_line=declaration.end_line,
                    match=match,
                )
            )

        current = existing_contents.get(destination_path)
        case_collision = existing_casefold.get(_path_collision_key(destination_path))
        if current == raw:
            if match_problem:
                disposition, reason = "declaration_conflict", match_problem
            elif primary_problem:
                disposition, reason = "declaration_conflict", primary_problem
            else:
                disposition, reason = "already_present", "Destination already has identical content."
        elif current is not None:
            disposition, reason = "path_conflict", "Destination path exists with different content."
        elif case_collision and case_collision != destination_path:
            disposition, reason = "path_conflict", f"Destination collides by case with {case_collision}."
        elif match_problem:
            disposition, reason = "declaration_conflict", match_problem
        elif primary_problem:
            disposition, reason = "declaration_conflict", primary_problem
        else:
            collision_paths: set[str] = set()
            for declaration in declarations:
                collision_paths.update(existing_names.get(declaration.full_name, set()))
                collision_paths.update(existing_names.get(declaration.short_name, set()))
            collision_paths.discard(destination_path)
            if collision_paths:
                disposition, reason = (
                    "declaration_conflict",
                    "Declaration already exists in " + ", ".join(sorted(collision_paths)) + ".",
                )
            else:
                disposition, reason = "add", "Destination path and declarations are available."
        planned.append(
            PlannedFile(
                source_path=source_file.source_path,
                destination_path=destination_path,
                disposition=disposition,
                reason=reason,
                content_sha256=digest,
                module_name=_module_name(destination_namespace, destination_path),
                declarations=planned_declarations,
                rewritten_content=content,
            )
        )

    incoming_names: dict[str, list[PlannedFile]] = defaultdict(list)
    incoming_paths: dict[str, list[PlannedFile]] = defaultdict(list)
    for item in planned:
        if item.disposition not in {"add", "already_present"}:
            continue
        if item.destination_path:
            incoming_paths[_path_collision_key(item.destination_path)].append(item)
        for declaration in item.declarations:
            incoming_names[declaration.full_name].append(item)
            incoming_names[declaration.short_name].append(item)
    ambiguous: set[int] = set()
    reasons: dict[int, str] = {}
    for name, members in incoming_names.items():
        distinct = {member.source_path for member in members}
        if len(distinct) > 1:
            for member in members:
                ambiguous.add(id(member))
                reasons[id(member)] = f"Incoming declaration {name} appears in multiple files."
    for _, members in incoming_paths.items():
        distinct = {member.source_path for member in members}
        if len(distinct) > 1:
            for member in members:
                ambiguous.add(id(member))
                reasons[id(member)] = "Incoming files map to the same destination path."
    for item in planned:
        if id(item) in ambiguous and item.disposition in {"add", "already_present"}:
            item.disposition = "declaration_conflict"
            item.reason = reasons[id(item)]

    return ImportPlan(
        source_namespace=inference.namespace,
        destination_namespace=destination_namespace,
        destination_snapshot=destination_snapshot(destination_repo, destination_namespace),
        files=planned,
        blocking_error=(
            {"code": "no_lean_files", "message": "The repository has no tracked Lean files."}
            if not source.files
            else None
        ),
    )


@dataclass
class Preview:
    id: str
    project_id: str
    project_namespace: str
    source_url: str
    source_ref: str | None
    source_commit_sha: str
    clone_path: Path
    inventory: SourceInventory
    plan: ImportPlan
    targets: list[TaggedTarget]
    expires_at: float
    consumed: bool = False


class PreviewRegistry:
    def __init__(self, ttl_seconds: int = 15 * 60):
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._items: dict[str, Preview] = {}

    def _cleanup_locked(self) -> None:
        now = time.monotonic()
        expired = [key for key, value in self._items.items() if value.expires_at <= now]
        for key in expired:
            preview = self._items.pop(key)
            shutil.rmtree(preview.clone_path.parent, ignore_errors=True)

    def add(
        self,
        *,
        project_id: str,
        project_namespace: str,
        source_url: str,
        source_ref: str | None,
        source_commit_sha: str,
        clone_path: Path,
        inventory: SourceInventory,
        plan: ImportPlan,
        targets: list[TaggedTarget],
    ) -> Preview:
        with self._lock:
            self._cleanup_locked()
            preview = Preview(
                id=str(uuid4()),
                project_id=project_id,
                project_namespace=project_namespace,
                source_url=source_url,
                source_ref=source_ref,
                source_commit_sha=source_commit_sha,
                clone_path=clone_path,
                inventory=inventory,
                plan=plan,
                targets=targets,
                expires_at=time.monotonic() + self.ttl_seconds,
            )
            self._items[preview.id] = preview
            return preview

    def get(self, preview_id: str, *, consume: bool = False) -> Preview:
        with self._lock:
            self._cleanup_locked()
            preview = self._items.get(preview_id)
            if preview is None or preview.consumed:
                raise ImportPlanningError("import_preview_expired", "Import preview expired; analyze again.")
            if consume:
                preview.consumed = True
            return preview

    def discard(self, preview_id: str) -> None:
        with self._lock:
            preview = self._items.pop(preview_id, None)
        if preview:
            shutil.rmtree(preview.clone_path.parent, ignore_errors=True)


preview_registry = PreviewRegistry()


def temporary_clone_path() -> Path:
    root = Path(tempfile.mkdtemp(prefix="lea-project-import-"))
    return root / "source"
