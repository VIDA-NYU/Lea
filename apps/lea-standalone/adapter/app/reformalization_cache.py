"""Host-owned baseline cache. Cached checks never certify a new candidate.

Only canonical checks/inspections populate this cache. Every attempt gets private
source and artifact copies; candidate and publication checks still run normally.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time

from lea import execution, interface, result_selection
from lea.check_evidence import (ReadSnapshot, freshness, file_hash,
                                _companions, prepared_environment, file_version)
from lea.runctx import run_context, current_run_key, current_should_stop
from . import db

VERSION = 1


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _implementation():
    from lea import check_evidence, lean_checks, check_runtime, tools, imports, lsp_daemon
    return {str(p): file_hash(p) for p in [Path(__file__), result_selection.INSPECTOR,
        *(Path(m.__file__) for m in (interface, result_selection, check_evidence, lean_checks,
                                    check_runtime, execution, lsp_daemon, tools, imports))]}


def _topology(root, budget):
    """New shadowing modules invalidate cached compiler resolution, too."""
    result = []
    next_poll = 0.0
    def poll():
        nonlocal next_poll
        # This loop only reads directory metadata. Thousands of directories must
        # not each open SQL snapshots to check cancellation/worker ownership.
        # Keep scanning cancellable; execution still has its own fresh gates.
        if time.monotonic() >= next_poll:
            budget.remaining('dependencies')
            next_poll = time.monotonic() + .1
    roots = [root / 'proofs', root / '.lake/build/lib/lean', root / '.lake/packages']
    poll()
    for base in roots:
        visited = set()
        for directory, dirs, files in os.walk(base, followlinks=True):
            poll()
            resolved = str(Path(directory).resolve())
            if resolved in visited:
                dirs[:] = []
                continue
            visited.add(resolved)
            children = sorted(d for d in dirs if d not in {'.git', '.lea', 'node_modules', '__pycache__'})
            for name in children:
                child = Path(directory) / name
                # Include aliases even when traversal deduplicates their target.
                result.append(('directory', str(child), str(child.resolve())))
            dirs[:] = [d for d in children if str((Path(directory) / d).resolve()) not in visited]
            for name in sorted(files):
                poll()
                if name.endswith(('.lean', '.olean')):
                    path = Path(directory) / name
                    result.append((str(path), str(path.resolve())))
                elif name.endswith(('.ir', '.so', '.dylib', '.dll', '.a')):
                    path = Path(directory) / name
                    result.append((str(path), str(path.resolve()), file_version(path)))
    budget.remaining('dependencies')
    return _digest(result)


def _context(project, root, repo, budget):
    from .recovery import _dependency_fingerprints
    commands = {name: shutil.which(name) for name in ('lake', 'lean')}
    return {'version': VERSION, 'root': str(root), 'repo': str(repo),
        'namespace': project['namespace'], 'implementation': _implementation(),
        'dependencies': _dependency_fingerprints(root), 'topology': _topology(root, budget),
        'executables': {name: {'path': str(Path(p).resolve()) if p else None,
                              'sha256': file_hash(p) if p else None} for name, p in commands.items()},
        'environment': {k: os.environ.get(k) for k in ('LEAN_PATH', 'LEAN_SRC_PATH', 'ELAN_TOOLCHAIN')}}


class Baseline:
    def __init__(self, project, root, repo, budget):
        self.project, self.root, self.repo, self.budget = project, root, repo, budget
        budget.remaining('dependencies')
        self.context = _context(project, root, repo, budget)
        key = _digest([project['id'], str(root), str(repo)])
        self.path = db.DB_PATH.parent / 'reformalization-cache' / (key + '.json')
        self.entries, self.hits, self.misses = {}, [], []
        self.snapshot = ReadSnapshot()
        try:
            saved = json.loads(self.path.read_text())
            if saved['context'] == self.context and isinstance(saved['entries'], dict):
                self.entries = saved['entries']
        except (OSError, ValueError, KeyError, TypeError):
            pass  # Disposable metadata: corruption requires actual checks.

    def _valid(self, path, entry, snapshot):
        try:
            checked, report = entry['check'], entry['report']
            if (checked['status'] != 'ok' or not report['complete']
                    or report['inspection'] != 'compiled_module'
                    or report['content_sha256'] != snapshot.observation(path)[0]
                    or not freshness(checked['execution'], path, snapshot=snapshot)[0]):
                return False
            env = checked['execution']['environment']
            for artifact, companions in _companions(env['files']).items():
                snapshot.artifact_sets[artifact] = companions
            artifacts = report['compiled_artifacts']
            stem = self.root / '.lake/build/lib/lean' / path.relative_to(self.root / 'proofs').with_suffix('')
            if not artifacts or str(stem) + '.olean' not in artifacts:
                return False
            ir = self.root / '.lake/build/ir' / path.relative_to(self.root / 'proofs').with_suffix('')
            for family in (stem, ir):
                snapshot.artifact_sets[str(family)] = sorted(p for p in artifacts if Path(p).parent == family.parent)
            for artifact, hashed in artifacts.items():
                candidate = Path(artifact)
                if (candidate.parent not in (stem.parent, ir.parent) or not candidate.name.startswith(stem.name + '.')
                        or candidate.is_symlink() or not hashed or snapshot.observation(candidate)[0] != hashed):
                    return False
            return snapshot.unchanged()
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return False

    def module(self, path):
        from .reformalizations import canonical_policy
        key = str(path.relative_to(self.root))
        entry = self.entries.get(key)
        self.budget.remaining('dependencies')
        # A rejected observation must not poison later valid observations.
        probe = ReadSnapshot()
        if entry and self._valid(path, entry, probe):
            self.hits.append(key)
        else:
            self.misses.append(key)
            with run_context(working_dir=str(self.repo), run_key=current_run_key(),
                             should_stop=current_should_stop()), execution.scope(canonical_policy(self.root, self.repo)):
                checked = interface.check(str(path), cold=True)
                if not (checked.execution or {}).get('environment'):
                    # Preserve preparation's strict dependency-acquisition errors.
                    with prepared_environment(path, path.read_text(), self.root, self.budget):
                        pass
                try:
                    report = result_selection.inspect_file(path, allow_errors=True)
                except ValueError:
                    report = None
            entry = {'check': {'status': checked.status, 'detail': checked.detail,
                               'execution': checked.execution}, 'report': report}
            self.entries[key] = entry
        return entry

    def seal(self, sources):
        """Pin a single stable snapshot after all intended builds have finished."""
        self.snapshot = ReadSnapshot()
        self.sources = {str(p.relative_to(self.root)): self.snapshot.observation(p)[0] for p in sources}
        for key in self.hits + self.misses:
            path = self.root / key
            entry = self.entries[key]
            if entry['check']['status'] == 'ok' and entry.get('report'):
                if not self._valid(path, entry, self.snapshot):
                    raise ValueError('Baseline inputs changed while preparing the retry: ' + key)
        current = _context(self.project, self.root, self.repo, self.budget)
        # New outputs are expected during cold builds. Executable inputs are not.
        if {k: v for k, v in current.items() if k != 'topology'} != {
                k: v for k, v in self.context.items() if k != 'topology'}:
            raise ValueError('Compiler or dependency context changed during preparation')
        if self.hits and current['topology'] != self.context['topology']:
            raise ValueError('Module resolution changed while reusing baseline checks')
        self.context = current
        if not self.snapshot.unchanged():
            raise ValueError('Baseline inputs changed during preparation')

    def copy_artifacts(self, staged_root):
        copied = set()
        for key in self.hits + self.misses:
            entry = self.entries[key]
            report = entry.get('report') or {}
            if entry['check']['status'] != 'ok' or not report.get('complete'):
                continue
            for raw, hashed in report.get('compiled_artifacts', {}).items():
                source = Path(raw)
                rel = source.relative_to(self.root)
                if rel in copied:
                    continue
                self.budget.remaining('dependencies')
                dest = staged_root / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, dest)  # Never share a writable inode.
                if file_hash(dest) != hashed:
                    raise ValueError('Compiled artifact changed while copying: ' + raw)
                # Lake's trace labels contain absolute source paths. Only its
                # metadata is relocated; compiler artifacts remain byte-identical.
                if dest.suffix == '.trace' or dest.name.endswith('.setup.json'):
                    value = json.loads(dest.read_text())
                    dest.write_text(json.dumps(_relocate(value, self.root, staged_root)))
                copied.add(rel)
        if not self.snapshot.unchanged():
            raise ValueError('Baseline changed while copying compiled artifacts')
        return len(copied)

    def save(self):
        if _context(self.project, self.root, self.repo, self.budget) != self.context:
            raise ValueError('Baseline resolution context changed during copying')
        if not self.snapshot.unchanged():
            raise ValueError('Baseline changed before caching preparation')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        saved = {'context': self.context, 'entries': self.entries}
        fd, temp = tempfile.mkstemp(dir=self.path.parent, prefix='.baseline-')
        try:
            with os.fdopen(fd, 'w') as out:
                json.dump(saved, out, separators=(',', ':'))
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp, self.path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)


def _relocate(value, root, staged_root):
    if isinstance(value, str) and value.startswith(str(root) + '/'):
        return str(staged_root) + value[len(str(root)):]
    if isinstance(value, list):
        return [_relocate(v, root, staged_root) for v in value]
    if isinstance(value, dict):
        return {k: _relocate(v, root, staged_root) for k, v in value.items()}
    return value
