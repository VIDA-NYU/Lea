"""Synthetic input evidence for unit tests that substitute compiler processes."""
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from lea.check_evidence import digest, environment_hash, lake_root, _configuration, source_identity
from lea.check_runtime import CheckText


def mocked_result(path, text):
    path = Path(path).resolve()
    if getattr(text, 'execution', None) or not path.is_file():
        return text
    environment = {'configuration': _configuration(lake_root(path)) if lake_root(path) else {}, 'files': {}}
    return CheckText(str(text), {'evidence_version': 1, 'content_sha256': digest(path.read_bytes()),
        'source_path': str(path), 'source_identity': source_identity(path), 'environment': environment,
        'environment_sha256': environment_hash(environment)}, path.read_text())


@contextmanager
def mocked_tool_inputs():
    from lea import registry, tools, agent
    build = registry.build_toolset
    def wrapped(*args, **kwargs):
        schemas, handlers = build(*args, **kwargs)
        if 'lean_check' in handlers:
            handlers = dict(handlers)
            original = handlers['lean_check']
            handlers['lean_check'] = lambda a: mocked_result(tools._run_relative_path(a['path']), original(a))
        return schemas, handlers
    with patch.object(registry, 'build_toolset', wrapped), patch.object(agent, 'build_toolset', wrapped):
        yield


@contextmanager
def unit_environment(path, content, root, budget):
    actual = lake_root(path)
    yield {'configuration': _configuration(actual) if actual else {}, 'files': {}}
