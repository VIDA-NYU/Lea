"""Versioned compiler inputs and workspace leases shared by all check consumers."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import subprocess
import threading

from .check_runtime import CheckBudget, CheckFailure, stop_process
from .imports import direct_imports, module_parts

PROTOCOL_VERSION = 1
_guard = threading.RLock()
_coordinators = {}
_hashes = {}
_resolutions = {}
_prepared = {}
_observed = {}


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def file_version(path):
    try:
        stat = Path(path).stat()
        return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]
    except OSError:
        return None


def file_hash(path):
    path = Path(path)
    try:
        stat = path.stat()
        identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        with _guard:
            previous = _hashes.get(str(path))
        if previous and previous[0] == identity:
            return previous[1]
        value = digest(path.read_bytes())
        with _guard:
            _hashes[str(path)] = (identity, value)
        return value
    except OSError:
        return None


def lake_root(path):
    path = Path(path).resolve()
    return next((p for p in [path if path.is_dir() else path.parent, *path.parents]
        if (p / 'lakefile.lean').exists() or (p / 'lakefile.toml').exists()), None)


class Coordinator:
    """Reentrant writer, shared readers; waits are cancellable and deadline-bound."""
    def __init__(self):
        self.condition = threading.Condition(threading.RLock())
        self.readers = {}
        self.writer = None
        self.depth = 0
        self.waiting_writers = 0

    @contextmanager
    def lease(self, exclusive=False, budget=None):
        budget = budget or CheckBudget()
        owner = threading.get_ident()
        with self.condition:
            if exclusive:
                self.waiting_writers += 1
            try:
                while self.writer not in (None, owner) or (
                    exclusive and sum(self.readers.values()) != self.readers.get(owner, 0)
                ) or (not exclusive and self.waiting_writers and self.writer != owner and owner not in self.readers):
                    self.condition.wait(timeout=min(.05, budget.remaining('queue')))
                budget.remaining('queue')
                if exclusive:
                    self.writer = owner
                    self.depth += 1
                else:
                    self.readers[owner] = self.readers.get(owner, 0) + 1
            finally:
                if exclusive:
                    self.waiting_writers -= 1
        try:
            yield
        finally:
            with self.condition:
                if exclusive:
                    self.depth -= 1
                    if not self.depth:
                        self.writer = None
                else:
                    self.readers[owner] -= 1
                    if not self.readers[owner]:
                        del self.readers[owner]
                self.condition.notify_all()


def coordinator(root):
    key = str(Path(root).resolve())
    with _guard:
        return _coordinators.setdefault(key, Coordinator())


@contextmanager
def mutation(path, *, budget=None):
    root = lake_root(path) or Path(path).resolve().parent
    with coordinator(root).lease(exclusive=True, budget=budget):
        yield


def _command(args, root, budget, content=None):
    proc = None
    try:
        proc = subprocess.Popen(args, cwd=root, stdin=subprocess.PIPE if content is not None else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
            errors='replace', start_new_session=True)
        first = True
        while True:
            try:
                stdout, stderr = proc.communicate(input=content if first else None,
                    timeout=min(.1, budget.remaining('dependencies')))
                return proc.returncode, stdout, stderr
            except subprocess.TimeoutExpired:
                first = False
    except OSError as exc:
        raise CheckFailure('dependencies_unavailable', str(exc), phase='dependencies', retryable=False) from exc
    finally:
        if proc:
            stop_process(proc)
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe:
                    pipe.close()


def _configuration(root):
    return {name: file_hash(root / name) for name in
        ('lean-toolchain', 'lakefile.lean', 'lakefile.toml', 'lake-manifest.json')}


def _resolve(path, code, root, budget, configuration):
    key = (str(root), str(path), digest(code.encode()), digest(configuration))
    with _guard:
        cached = _resolutions.get(key)
    if cached is not None:
        return cached
    values = []
    for option in ('--src-deps', '--deps'):
        command = ['lake', 'env', 'lean'] if root else ['lean']
        status, output, error = _command([*command, option, '--stdin', str(path)],
            root or path.parent, budget, code)
        if status:
            raise CheckFailure('dependency_resolution', error or output, phase='dependencies', retryable=False)
        values.append([Path(line.strip()).absolute() for line in output.splitlines() if line.strip()])
    if len(values[0]) != len(values[1]):
        raise CheckFailure('dependency_resolution', 'Lean returned inconsistent dependency paths',
            phase='dependencies', retryable=False)
    result = list(zip(*values))
    with _guard:
        _resolutions[key] = result
    return result


def _identity(path, root):
    if root and path.is_relative_to(root):
        return 'workspace:' + path.relative_to(root).as_posix()
    for marker in ('src/lean/', 'lib/lean/'):
        if marker in path.as_posix():
            return 'toolchain:' + marker + path.as_posix().split(marker, 1)[1]
    return 'external:' + str(path)


def manifest(path, content, root, budget=None):
    """Resolve direct imports with Lean, then traverse actual module source roots.

    Source roots learned from the compiler avoid launching Lean per Mathlib module.
    Unknown roots are resolved by the compiler rather than guessed from the catalog.
    """
    budget = budget or CheckBudget()
    root = Path(root).resolve() if root else None
    if root is None:
        # No package resolver is available: never certify imported standalone files.
        if any(module_parts(name)[0] not in {'Init', 'Lean', 'Std'} for name in direct_imports(content)):
            raise CheckFailure('dependency_resolution', 'Imported files require a Lake workspace', retryable=False)
    configuration = _configuration(root) if root else {}
    files, roots, seen = {}, {}, set()
    def record(candidate, kind):
        files[_identity(candidate, root)] = {'path': str(candidate), 'sha256': file_hash(candidate),
            'kind': kind, 'version': file_version(candidate)}
    def add(source, compiled, module):
        for kind, candidate in [('source', source), ('compiled', compiled)]:
            record(candidate, kind)
            if kind == 'compiled':
                for companion in candidate.parent.glob(candidate.name + '.*'):
                    record(companion, kind)
        # Pin the actual compiler installation as well as the toolchain selector.
        if _identity(source, root).startswith('toolchain:'):
            installation = Path(source.as_posix().split('/src/lean/', 1)[0])
            record(installation / 'bin/lean', 'compiler')
        elif root and source.is_relative_to(root / '.lake/packages'):
            package = root / '.lake/packages' / source.relative_to(root / '.lake/packages').parts[0]
            for name in ('lean-toolchain', 'lakefile.lean', 'lakefile.toml', 'lake-manifest.json'):
                candidate = package / name
                if candidate.is_file():
                    record(candidate, 'configuration')
        if module:
            parts = module_parts(module)
            roots[parts[0]] = (source.parents[len(parts)-1], compiled.parents[len(parts)-1])
        if source in seen:
            return
        seen.add(source)
        # Built-in libraries are an immutable installation, identified by its
        # toolchain and the resolved source/artifact hashes, not writable projects.
        if _identity(source, root).startswith('toolchain:'):
            return
        try:
            code = source.read_text()
        except (OSError, UnicodeError):
            raise CheckFailure('dependency_resolution', f'Cannot read imported module {source}', retryable=False)
        if root and source.is_relative_to(root) and '.lake' not in source.relative_to(root).parts:
            local = _resolve(source, code, root, budget, configuration)
            imports = direct_imports(code)
            for dep_source, dep_compiled in local:
                inferred = next((name for name in imports if dep_source.as_posix().endswith('/'.join(module_parts(name)) + '.lean')), 'Init')
                add(dep_source, dep_compiled, inferred)
            return
        for name in direct_imports(code):
            prefix = module_parts(name)[0]
            if prefix in roots:
                src, obj = roots[prefix]
                rel = Path(*module_parts(name))
                dep_source, dep_compiled = src / Path(str(rel) + '.lean'), obj / Path(str(rel) + '.olean')
                if dep_source.is_file():
                    add(dep_source, dep_compiled, name)
                    continue
            resolved = _resolve(path, 'import ' + name + '\n', root, budget, configuration)
            for dep_source, dep_compiled in resolved:
                inferred = name if dep_source.name == module_parts(name)[-1] + '.lean' else 'Init'
                add(dep_source, dep_compiled, inferred)
    resolved = _resolve(path, content, root, budget, configuration)
    names = direct_imports(content)
    for source, compiled in resolved:
        name = next((name for name in names if source.as_posix().endswith('/'.join(module_parts(name)) + '.lean')), 'Init')
        add(source, compiled, name)
    return {'configuration': configuration, 'files': files,
        'configuration_versions': {name: file_version(root / name) for name in configuration} if root else {}}


def observed_changed(root, current):
    previous = _observed.get(str(root), {})
    if previous.get('configuration') != current['configuration']:
        return True
    return any(previous.get('files', {}).get(name) not in (None, record['sha256'])
        for name, record in current['files'].items() if record['kind'] == 'compiled')


def observe(root, current):
    previous = _observed.setdefault(str(root), {'files': {}})
    previous['configuration'] = current['configuration']
    previous['files'].update({name: record['sha256'] for name, record in current['files'].items()
        if record['kind'] == 'compiled'})


def environment_hash(value):
    return digest({'configuration': value['configuration'],
        'files': {k: v['sha256'] for k, v in value['files'].items()}})


@contextmanager
def prepared_environment(path, content, root, budget):
    root = Path(root).resolve() if root else None
    lock = coordinator(root or path.parent)
    while True:
        try:
            with lock.lease(budget=budget):
                current = manifest(path, content, root, budget)
                key = (str(root), tuple(direct_imports(content)))
                hashed = environment_hash(current)
                if root is None or (_prepared.get(key) == hashed and not observed_changed(root, current)):
                    yield current
                    return
        except CheckFailure as exc:
            if exc.kind != 'dependency_resolution' or root is None or not direct_imports(content):
                raise
            # Lean cannot resolve a newly created library prefix until Lake has
            # produced its first artifact. Bootstrap only the requested imports.
            with lock.lease(exclusive=True, budget=budget):
                targets = [name for name in direct_imports(content) if name.split('.')[0] not in {'Init', 'Lean', 'Std'}]
                if not targets:
                    raise
                status, output, error = _command(['lake', 'build', *targets], root, budget)
                if status:
                    raise CheckFailure('dependency_build', (output + '\n' + error).strip(),
                        phase='dependencies', retryable=False)
                from .lsp_daemon import mark_stale
                mark_stale(str(root))
                current = manifest(path, content, root, budget)
                _prepared[(str(root), tuple(direct_imports(content)))] = environment_hash(current)
                observe(root, current)
            continue
        with lock.lease(exclusive=True, budget=budget):
            current = manifest(path, content, root, budget)
            if _prepared.get(key) != environment_hash(current) or observed_changed(root, current):
                before_sources = {k: v['sha256'] for k, v in current['files'].items() if v['kind'] == 'source'}
                # Lake follows exactly these roots and their transitive dependencies.
                targets = [name for name in direct_imports(content) if name.split('.')[0] not in {'Init', 'Lean', 'Std'}]
                if targets and _prepared.get(key) != environment_hash(current):
                    status, output, error = _command(['lake', 'build', *targets], root, budget)
                    if status:
                        raise CheckFailure('dependency_build', (output + '\n' + error).strip(),
                            phase='dependencies', retryable=False)
                from .lsp_daemon import mark_stale
                mark_stale(str(root))
                current = manifest(path, content, root, budget)
                if before_sources != {k: v['sha256'] for k, v in current['files'].items() if v['kind'] == 'source'}:
                    raise CheckFailure('source_changed', 'Imported source changed during its build', retryable=False)
                _prepared[key] = environment_hash(current)
                observe(root, current)


def source_identity(path):
    path = Path(path).resolve()
    root = lake_root(path)
    return path.relative_to(root).as_posix() if root else str(path)


def freshness(execution, path, content=None):
    if not execution or execution.get('evidence_version') != PROTOCOL_VERSION or not execution.get('environment'):
        return False, 'missing_evidence'
    path = Path(path)
    try:
        if execution.get('source_identity') != source_identity(path):
            return False, 'source_changed'
        current = path.read_bytes() if content is None else content.encode() if isinstance(content, str) else content
        if digest(current) != execution.get('content_sha256'):
            return False, 'source_changed'
        env = execution['environment']
        root = lake_root(path)
        if env['configuration'] != (_configuration(root) if root else {}):
            return False, 'environment_changed'
        if root and env.get('configuration_versions') and any(file_version(root / name) != version for name, version in env['configuration_versions'].items()):
            return False, 'environment_changed'
        for identity, record in env['files'].items():
            actual = root / identity[len('workspace:'):] if root and identity.startswith('workspace:') else record['path']
            if file_hash(actual) != record['sha256'] or record['sha256'] is None:
                return False, 'dependency_changed'
            if record.get('version') and file_version(actual) != record['version']:
                return False, 'dependency_changed'
        if environment_hash(env) != execution.get('environment_sha256'):
            return False, 'missing_evidence'
    except (OSError, KeyError, TypeError, ValueError):
        return False, 'unavailable'
    return True, None


def public_execution(execution):
    """Keep dependency manifests in evidence records, not every UI activity frame."""
    return {k: v for k, v in (execution or {}).items() if k not in {'environment', 'source_path', 'source_identity'}}
