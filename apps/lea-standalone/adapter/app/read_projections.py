"""A bounded filesystem snapshot for synchronous, read-only status projections."""
from contextvars import ContextVar
from functools import wraps

from fastapi import HTTPException


_active = ContextVar('proof_read_snapshot', default=None)
_artifacts = ContextVar('artifact_read_snapshots', default=None)


def decode_evidence(raw):
    import json
    snapshot = _active.get()
    return snapshot.decode(raw) if snapshot else json.loads(raw)


def current_snapshot():
    return _active.get()


def current_artifact_snapshots():
    return _artifacts.get()


def read_projection(fn):
    """Nested projections share work; changed inputs cause a fresh read or 503.

    No successful freshness decision survives the response. In particular this
    never supplies a cached decision to compilation, completion or persistence.
    """
    @wraps(fn)
    def read(*args, **kwargs):
        if _active.get() is not None:
            return fn(*args, **kwargs)
        for _ in range(2):
            from lea.check_evidence import ReadSnapshot
            snapshot = ReadSnapshot()
            token = _active.set(snapshot)
            artifact_token = _artifacts.set({})
            try:
                result = fn(*args, **kwargs)
                if snapshot.unchanged():
                    return result
            finally:
                _artifacts.reset(artifact_token)
                _active.reset(token)
        raise HTTPException(status_code=503,
            detail={'error': 'proof_environment_changing', 'message': 'Proof inputs changed while loading; retry.'},
            headers={'Retry-After': '1'})
    return read


def __getattr__(name):
    if name=='ReadSnapshot':
        from lea.check_evidence import ReadSnapshot
        return ReadSnapshot
    raise AttributeError(name)
