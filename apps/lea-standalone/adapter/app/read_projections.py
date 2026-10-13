"""A bounded filesystem snapshot for synchronous, read-only status projections."""
from contextvars import ContextVar
from functools import wraps

from fastapi import HTTPException
from lea.check_evidence import ReadSnapshot

_active = ContextVar('proof_read_snapshot', default=None)


def decode_evidence(raw):
    import json
    snapshot = _active.get()
    return snapshot.decode(raw) if snapshot else json.loads(raw)


def current_snapshot():
    return _active.get()


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
            snapshot = ReadSnapshot()
            token = _active.set(snapshot)
            try:
                result = fn(*args, **kwargs)
                if snapshot.unchanged():
                    return result
            finally:
                _active.reset(token)
        raise HTTPException(status_code=503,
            detail={'error': 'proof_environment_changing', 'message': 'Proof inputs changed while loading; retry.'},
            headers={'Retry-After': '1'})
    return read
