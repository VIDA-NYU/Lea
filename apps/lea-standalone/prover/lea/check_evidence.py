"""Versioned compiler inputs and workspace leases shared by all check consumers."""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
from bisect import bisect_left
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from . import execution as command_executor
import threading

from .check_runtime import CheckBudget, CheckFailure, stop_process
from .imports import direct_imports, module_parts, without_comments, IMPORT_COMMAND_RE

PROTOCOL_VERSION = 1
_guard = threading.RLock()
_coordinators = {}
_hashes = {}
_resolutions = {}
_prepared = {}
_observed = {}
_source_imports = {}


def _artifact_family(path, directories):
    """Observe a directory once per operation, including unknown Lean sidecars."""
    path = Path(path)
    parent = str(path.parent)
    if parent not in directories:
        try:
            directories[parent] = sorted(os.listdir(parent))
        except (FileNotFoundError, NotADirectoryError):
            directories[parent] = []
    names = directories[parent]
    prefix = path.name + '.'
    index = bisect_left(names, prefix)
    result = []
    while index < len(names) and names[index].startswith(prefix):
        result.append(os.path.join(parent, names[index]))
        index += 1
    return result


def _artifact_families(paths):
    directories = {}
    return {path: _artifact_family(path, directories) for path in paths}


def _imports_for_source(path, observation):
    # Cache parsing by checked bytes, never by a timestamp or module name alone.
    key = (str(path), observation['sha256'])
    with _guard:
        cached = _source_imports.get(key)
    if cached is not None:
        return cached
    code = path.read_bytes()
    if digest(code) != observation['sha256'] or file_version(path) != observation['version']:
        raise CheckFailure('source_changed', 'Imported source changed during dependency discovery', retryable=False)
    imports = direct_imports(code.decode('utf-8'))
    with _guard:
        _source_imports[key] = imports
    return imports


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def file_version(path):
    try:
        stat = Path(path).stat()
        return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]
    except OSError:
        return None


def file_observation(path):
    """Hash and identity from the same observation; reject a concurrent rewrite."""
    path = Path(path)
    identity = None
    try:
        stat = path.stat()
        identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        with _guard:
            previous = _hashes.get(str(path))
        if previous and previous[0] == identity:
            return previous[1], list(identity)
        value = digest(path.read_bytes())
        if file_version(path) != list(identity):
            return None, None
        with _guard:
            _hashes[str(path)] = (identity, value)
        return value, list(identity)
    except OSError:
        return None, (list(identity) if identity and file_version(path) == list(identity) else None)


def file_hash(path):
    return file_observation(path)[0]


class ReadSnapshot:
    """Deduplicate observations inside one read, never across returned responses.

    A caller must validate `unchanged()` before publishing its projection. Compiler
    checks and evidence persistence do not opt into this read-only optimization.
    """
    def __init__(self):
        self.files = {}
        self.executions = {}
        self.environments = {}
        self.topology = {}
        self.artifact_sets = {}
        self.checks = {}

    def observation(self, path):
        key = str(path)
        if key not in self.files:
            observed = file_observation(path)
            if observed[0] is None:
                # A missing optional configuration and an unreadable/racing file
                # both have no digest. Only genuine absence may match old absence.
                try:
                    Path(path).stat()
                except FileNotFoundError:
                    pass
                else:
                    raise OSError(f'Cannot observe stable input bytes: {path}')
            self.files[key] = observed
        return self.files[key]

    def decode(self, raw):
        if raw not in self.executions:
            self.executions[raw] = json.loads(raw)
        return self.executions[raw]

    def environment_valid(self, execution):
        key = id(execution)
        if key not in self.environments:
            self.environments[key] = (execution,
                environment_hash(execution['environment']) == execution.get('environment_sha256'))
        return self.environments[key][1]

    def unchanged(self):
        return (all(file_version(path) == observed[1] for path, observed in self.files.items())
            and all(file_version(path) == version for path, version in self.topology.items())
            and _artifact_families(self.artifact_sets) == self.artifact_sets)

    def watch_root(self, path, root):
        # A newly created, nearer Lake file changes module resolution even when
        # all files in the old manifest still exist and have identical bytes.
        for parent in Path(path).resolve().parents:
            for name in ('lakefile.lean', 'lakefile.toml'):
                candidate = str(parent / name)
                if candidate not in self.topology:
                    self.topology[candidate] = file_version(candidate)
            if parent == root:
                break


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
        proc = command_executor.popen(args, cwd=root, stdin=subprocess.PIPE if content is not None else None,
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


def _dependency_header(code):
    """Compiler import resolution is independent of edits after the header.

    Preserve modifiers, module and prelude commands. Unrecognized import syntax
    falls back to the complete input rather than dropping a dependency.
    """
    header = []
    for line in without_comments(code).splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped in {'module', 'prelude'} or IMPORT_COMMAND_RE.fullmatch(line):
            header.append(line)
        else:
            if any(word in stripped.split() for word in ('import', 'prelude', 'module')):
                return code
            break
    return '\n'.join(header) + '\n'


def _resolve(path, code, root, budget, configuration):
    code = _dependency_header(code)
    key = (str(root), str(path), digest(code.encode()), digest(configuration))
    with _guard:
        cached = _resolutions.get(key)
    if cached is not None:
        return cached
    values = []
    for option in ('--src-deps', '--deps'):
        command = ['lake', 'env', 'lean'] if root else ['lean']
        with budget.measure('dependency_resolution'):
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
    text = path.as_posix()
    if root:
        prefix = root.as_posix().rstrip('/') + '/'
        if text.startswith(prefix):
            return 'workspace:' + text[len(prefix):]
    for marker in ('src/lean/', 'lib/lean/'):
        if marker in text:
            return 'toolchain:' + marker + text.split(marker, 1)[1]
    return 'external:' + str(path)


def manifest(path, content, root, budget=None):
    """Resolve direct imports with Lean, then traverse actual module source roots.

    Source roots learned from the compiler avoid launching Lean per Mathlib module.
    Unknown roots are resolved by the compiler rather than guessed from the catalog.
    """
    from .runctx import current_should_stop
    budget = budget or CheckBudget(should_stop=current_should_stop())
    budget.remaining('dependencies')
    root = Path(root).resolve() if root else None
    if root is None:
        # No package resolver is available: never certify imported standalone files.
        if any(module_parts(name)[0] not in {'Init', 'Lean', 'Std'} for name in direct_imports(content)):
            raise CheckFailure('dependency_resolution', 'Imported files require a Lake workspace', retryable=False)
    configuration_observations = {name: file_observation(root / name) for name in
        ('lean-toolchain', 'lakefile.lean', 'lakefile.toml', 'lake-manifest.json')} if root else {}
    configuration = {name: value[0] for name, value in configuration_observations.items()}
    files, roots, seen = {}, {}, set()
    directories, recorded = {}, set()
    root_prefix = root.as_posix().rstrip('/') + '/' if root else ''
    packages_prefix = root_prefix + '.lake/packages/' if root else ''
    last_poll = time.monotonic()
    def poll():
        nonlocal last_poll
        now = time.monotonic()
        if now - last_poll >= .05:
            budget.remaining('dependencies')
            # A SQL stop poll can itself exceed this interval under load. Start
            # the next interval after it returns, or every file polls SQL again.
            last_poll = time.monotonic()
    def record(candidate, kind):
        poll()
        if (candidate, kind) in recorded:
            return
        recorded.add((candidate, kind))
        hashed, version = file_observation(candidate)
        files[_identity(candidate, root)] = {'path': str(candidate), 'sha256': hashed,
            'kind': kind, 'version': version}
    def add(source, compiled, module):
        poll()
        if module:
            parts = module_parts(module)
            roots[parts[0]] = (source.parents[len(parts)-1], compiled.parents[len(parts)-1])
        # Import diamonds must not re-observe every artifact family on each edge.
        # Keep distinct compiler-resolved artifacts even if their source is shared.
        pair = (source, compiled)
        if pair in seen:
            return
        seen.add(pair)
        source_key = _identity(source, root)
        source_text = source.as_posix()
        toolchain_source = source_key.startswith('toolchain:')
        for kind, candidate in [('source', source), ('compiled', compiled)]:
            record(candidate, kind)
            if kind == 'compiled':
                for companion in _artifact_family(candidate, directories):
                    record(Path(companion), kind)
        # Pin the actual compiler installation as well as the toolchain selector.
        if toolchain_source:
            installation = Path(source.as_posix().split('/src/lean/', 1)[0])
            record(installation / 'bin/lean', 'compiler')
        elif packages_prefix and source_text.startswith(packages_prefix):
            package = root / '.lake/packages' / source_text[len(packages_prefix):].split('/', 1)[0]
            for name in ('lean-toolchain', 'lakefile.lean', 'lakefile.toml', 'lake-manifest.json'):
                candidate = package / name
                if candidate.is_file():
                    record(candidate, 'configuration')
        # Built-in libraries are an immutable installation, identified by its
        # toolchain and the resolved source/artifact hashes, not writable projects.
        if toolchain_source:
            return
        try:
            imports = _imports_for_source(source, files[source_key])
        except (OSError, UnicodeError):
            raise CheckFailure('dependency_resolution', f'Cannot read imported module {source}', retryable=False)
        if root_prefix and source_text.startswith(root_prefix) and '.lake' not in source_text[len(root_prefix):].split('/'):
            local = _resolve(source, source.read_text(), root, budget, configuration)
            for dep_source, dep_compiled in local:
                inferred = next((name for name in imports if dep_source.as_posix().endswith('/'.join(module_parts(name)) + '.lean')), 'Init')
                add(dep_source, dep_compiled, inferred)
            return
        for name in imports:
            prefix = module_parts(name)[0]
            if prefix in roots:
                src, obj = roots[prefix]
                rel = Path(*module_parts(name))
                dep_source, dep_compiled = src / Path(str(rel) + '.lean'), obj / Path(str(rel) + '.olean')
                if dep_source.is_file():
                    add(dep_source, dep_compiled, name)
                    continue
            # A probe contains only an absolute import. Give it one stable
            # filename per workspace so sibling targets reuse compiler discovery.
            probe = root / 'LeaDependencyImports.lean' if root else path
            resolved = _resolve(probe, 'import ' + name + '\n', root, budget, configuration)
            for dep_source, dep_compiled in resolved:
                inferred = name if dep_source.name == module_parts(name)[-1] + '.lean' else 'Init'
                add(dep_source, dep_compiled, inferred)
    resolved = _resolve(path, content, root, budget, configuration)
    names = direct_imports(content)
    for source, compiled in resolved:
        name = next((name for name in names if source.as_posix().endswith('/'.join(module_parts(name)) + '.lean')), 'Init')
        add(source, compiled, name)
    budget.remaining('dependencies')
    return {'configuration': configuration, 'files': files,
        'configuration_versions': {name: value[1] for name, value in configuration_observations.items()}}


def _runtime_files(current):
    return {name: {**record, 'resolved_path': str(Path(record['path']).resolve())}
        for name, record in current['files'].items()
        if record['kind'] in {'source', 'compiled', 'compiler', 'configuration'}}


def _runtime_record_changed(old, current):
    # Source bytes govern build preparation and proof evidence. A server needs
    # retirement when its loaded artifacts or their resolution identities change.
    return (any(old[key] != current[key] for key in ('kind', 'path', 'resolved_path'))
        or current['kind'] != 'source' and old['sha256'] != current['sha256'])


def _companions(files):
    paths = {record['path'] for record in files.values()}
    result = {path: [] for path in paths if path.endswith('.olean')}
    for path in paths:
        base, separator, _ = path.rpartition('.olean.')
        if separator and base + '.olean' in result:
            result[base + '.olean'].append(path)
    return {path: sorted(values) for path, values in result.items()}


def observe(root, current, *, generation):
    """Remember inputs before dispatch, including requests that subsequently fail.

    A new generation starts with no predecessor inputs. Unknown admissions remain
    conservative: a later build cannot establish that such a server is reusable.
    """
    if current is not None:
        files = _runtime_files(current)
        configuration_paths = {name: str((Path(root) / name).resolve()) for name in current['configuration']}
        companions = _companions(files)
        if _artifact_families(companions) != companions:
            raise CheckFailure('stale_generation', 'Lean artifacts changed during admission')
    with _guard:
        previous = _observed.get(str(root), {})
        if previous.get('generation') != generation:
            previous = {'generation': generation, 'files': {}, 'companions': {}}
        if current is None:
            _observed[str(root)] = {**previous, 'unknown': True}
            return
        if (previous.get('invalidated') or previous.get('unknown')
                or 'configuration' in previous and previous['configuration'] != current['configuration']
                or 'configuration_paths' in previous and previous['configuration_paths'] != configuration_paths
                or any(
                name in previous['files'] and _runtime_record_changed(previous['files'][name], record)
                for name, record in files.items())):
            raise CheckFailure('stale_generation', 'Lean admission inputs no longer match the serving generation')
        _observed[str(root)] = {**previous, 'configuration': dict(current['configuration']),
            'configuration_paths': configuration_paths,
            'files': {**previous['files'], **files},
            'companions': {**previous['companions'], **companions}}


def observed_changed(root, current=None):
    """Validate the serving generation's entire admitted environment, not just
    the build's target closure. Metadata changes require fresh hashes, not retirement.
    Callers hold a workspace lease; mutations reconcile under its exclusive lease.
    """
    try:
        return _observed_changed(root, current)
    except (OSError, KeyError, TypeError, ValueError, RuntimeError):
        return True  # Uncertain compatibility must never preserve a serving cache.


def _observed_changed(root, current):
    from .lsp_daemon import runtime_snapshot
    generation = runtime_snapshot(root)['generation']
    if generation is None:
        return False
    with _guard:
        previous = _observed.get(str(root), {})
    if previous.get('generation') != generation or previous.get('unknown'):
        return True
    if previous.get('invalidated'):
        return False  # Retirement is already scheduled before the next admission.
    admitted = _runtime_files(current) if current is not None else {}
    if current is not None:
        if previous['configuration'] != current['configuration']:
            return True
        for name, record in admitted.items():
            old = previous['files'].get(name)
            if old and _runtime_record_changed(old, record):
                return True
    snapshot = ReadSnapshot()
    for name, hashed in previous['configuration'].items():
        if str((Path(root) / name).resolve()) != previous['configuration_paths'][name]:
            return True
        if snapshot.observation(Path(root) / name)[0] != hashed:
            return True
    for name, record in previous['files'].items():
        newer = admitted.get(name)
        if newer and not _runtime_record_changed(record, newer) and newer['sha256'] is not None:
            # The current manifest has already resolved and hashed these bytes.
            # Revalidate their identities at the end instead of resolving and
            # hashing the same overlapping closure a second time.
            snapshot.files[record['path']] = (newer['sha256'], newer['version'])
            continue
        path = Path(record['path'])
        if str(path.resolve()) != record['resolved_path']:
            return True
        if record['kind'] == 'source':
            continue
        if record['sha256'] is None:
            return True
        if snapshot.observation(path)[0] != record['sha256']:
            return True
    for path, companions in previous['companions'].items():
        snapshot.topology[str(Path(path).parent)] = file_version(Path(path).parent)
    if _artifact_families(previous['companions']) != previous['companions']:
        return True
    return not snapshot.unchanged()


def reconcile_daemon(root, current=None):
    """Reconcile even failed/partial builds, before releasing the writer lease."""
    if observed_changed(root, current):
        from .lsp_daemon import mark_stale, runtime_snapshot
        generation = runtime_snapshot(root)['generation']
        mark_stale(str(root))
        with _guard:
            _observed[str(root)] = {**_observed.get(str(root), {}),
                'generation': generation, 'invalidated': True, 'unknown': False}


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
                try:
                    with budget.measure('dependency_build'):
                        status, output, error = _command(['lake', 'build', *targets], root, budget)
                finally:
                    reconcile_daemon(root)
                if status:
                    raise CheckFailure('dependency_build', (output + '\n' + error).strip(),
                        phase='dependencies', retryable=False)
                current = manifest(path, content, root, budget)
                _prepared[(str(root), tuple(direct_imports(content)))] = environment_hash(current)
            continue
        with ExitStack() as prepared_reader:
            with lock.lease(exclusive=True, budget=budget):
                current = manifest(path, content, root, budget)
                if _prepared.get(key) != environment_hash(current) or observed_changed(root, current):
                    before_sources = {k: v['sha256'] for k, v in current['files'].items() if v['kind'] == 'source'}
                    # Lake follows exactly these roots and their transitive dependencies.
                    targets = [name for name in direct_imports(content) if name.split('.')[0] not in {'Init', 'Lean', 'Std'}]
                    if targets and _prepared.get(key) != environment_hash(current):
                        try:
                            with budget.measure('dependency_build'):
                                status, output, error = _command(['lake', 'build', *targets], root, budget)
                        finally:
                            reconcile_daemon(root)
                        if status:
                            raise CheckFailure('dependency_build', (output + '\n' + error).strip(),
                                phase='dependencies', retryable=False)
                    current = manifest(path, content, root, budget)
                    reconcile_daemon(root, current)
                    if before_sources != {k: v['sha256'] for k, v in current['files'].items() if v['kind'] == 'source'}:
                        raise CheckFailure('source_changed', 'Imported source changed during its build', retryable=False)
                    _prepared[key] = environment_hash(current)
                # Acquire the shared lease before releasing the writer. No
                # coordinated mutation can slip between build and compilation.
                prepared_reader.enter_context(lock.lease(budget=budget))
            yield current
            return


def source_identity(path):
    path = Path(path).resolve()
    root = lake_root(path)
    return path.relative_to(root).as_posix() if root else str(path)


def freshness(execution, path, content=None, *, snapshot=None):
    if snapshot is None:
        observation = ReadSnapshot()
        result = _freshness(execution, path, content, snapshot=observation)
        return result if observation.unchanged() else (False, 'unavailable')
    if snapshot and content is None:
        key = (id(execution), str(path))
        if key not in snapshot.checks:
            snapshot.checks[key] = (execution, _freshness(execution, path, content, snapshot=snapshot))
        return snapshot.checks[key][1]
    return _freshness(execution, path, content, snapshot=snapshot)


def _freshness(execution, path, content=None, *, snapshot=None):
    if not execution or execution.get('evidence_version') != PROTOCOL_VERSION or not execution.get('environment'):
        return False, 'missing_evidence'
    path = Path(path)
    try:
        root = lake_root(path)
        if snapshot:
            snapshot.watch_root(path, root)
        identity = path.resolve().relative_to(root).as_posix() if root else str(path.resolve())
        if execution.get('source_identity') != identity:
            return False, 'source_changed'
        observe = snapshot.observation if snapshot else file_observation
        current_hash = observe(path)[0] if content is None else digest(
            content.encode() if isinstance(content, str) else content)
        if current_hash != execution.get('content_sha256'):
            return False, 'source_changed'
        env = execution['environment']
        # Observe absent configuration files too: creating one changes the environment.
        configuration = {name: observe(root / name)[0] for name in
            ('lean-toolchain', 'lakefile.lean', 'lakefile.toml', 'lake-manifest.json')} if root else {}
        if env['configuration'] != configuration:
            return False, 'environment_changed'
        root_path = str(root) if root else None
        for identity, record in env['files'].items():
            actual = os.path.join(root_path, identity[len('workspace:'):]) if root and identity.startswith('workspace:') else record['path']
            observed_hash, _ = observe(actual)
            if observed_hash != record['sha256'] or record['sha256'] is None:
                return False, 'dependency_changed'
        if not (snapshot.environment_valid(execution) if snapshot else
                environment_hash(env) == execution.get('environment_sha256')):
            return False, 'missing_evidence'
    except (OSError, KeyError, TypeError, ValueError):
        return False, 'unavailable'
    return True, None


def execution_snapshot(execution, path):
    """Strict operation-local guard, independent of historical content validity.

    Retain identities from manifest capture through completion. A changed-and-
    restored input must not certify a compiler run that could have seen mixed bytes.
    """
    snapshot = ReadSnapshot()
    valid, _ = freshness(execution, path, snapshot=snapshot)
    env = execution['environment']
    root = lake_root(path)
    if root:
        valid = valid and all(snapshot.observation(root / name)[1] == version
            for name, version in env.get('configuration_versions', {}).items())
    valid = valid and all(not record.get('version') or
        snapshot.observation(record['path'])[1] == record['version']
        for record in env['files'].values())
    for artifact, companions in _companions(env['files']).items():
        # Track the relevant artifact family. Unrelated files created by kernel
        # replay or another module's preparation do not change these inputs.
        snapshot.artifact_sets[artifact] = companions
    if not valid or not snapshot.unchanged():
        raise CheckFailure('source_changed', 'Compiler inputs changed while preparing the check', retryable=False)
    return snapshot


@contextmanager
def execution_guard(execution, path, root):
    snapshot = execution_snapshot(execution, path)
    try:
        yield snapshot
    finally:
        generation = execution.get('daemon_generation')
        if generation and not snapshot.unchanged():
            # Even a rejected/cancelled request may have loaded mixed imports.
            # Retire its generation while the surrounding reader lease is held.
            from .lsp_daemon import mark_stale
            mark_stale(root, generation=generation)


def public_execution(execution):
    """Keep dependency manifests in evidence records, not every UI activity frame."""
    return {k: v for k, v in (execution or {}).items() if k not in {'environment', 'source_path', 'source_identity'}}
