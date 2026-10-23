"""Bounded worker-owned diagnostics; the API sees only published SQL summaries."""
from collections import deque
from datetime import datetime, timezone
import faulthandler
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import threading
import traceback

from . import db
from .public_errors import redact

_recent = deque(maxlen=20)
_guard = threading.RLock()
_events = None
_handlers = []
_native = None
_identity = None


def safe_text(value, limit=8000):
    # Redact before truncating, keeping the exception at the end of a traceback.
    return '\n'.join(redact(line) for line in str(value).splitlines())[-limit:]


class _Formatter(logging.Formatter):
    def format(self, record):
        return safe_text(super().format(record))


class _Recent(logging.Handler):
    def emit(self, record):
        if record.levelno >= logging.WARNING:
            event('worker.log', severity=record.levelname.lower(), logger=record.name,
                  message=safe_text(record.getMessage(), 1500))


def _tail(path):
    try:
        with path.open('rb') as stream:
            stream.seek(max(0, path.stat().st_size - 65536))
            lines = stream.read(65536).decode('utf-8', 'replace').splitlines()
        result = []
        for line in lines:
            try: result.append(json.loads(line))
            except ValueError: pass
        return result[-20:]
    except OSError:
        return []


def setup(identity):
    global _events, _identity, _native
    close()
    _identity = identity
    directory = db.DB_PATH.parent / 'worker-diagnostics'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    _recent.clear()
    previous = []
    for name in ('worker-events.jsonl.1', 'worker-events.jsonl', 'supervisor-events.jsonl'):
        previous.extend(_tail(directory / name))
    _recent.extend(sorted(previous, key=lambda value: value.get('at', ''))[-20:])
    _events = RotatingFileHandler(directory / 'worker-events.jsonl', maxBytes=512*1024, backupCount=2)
    _events.setFormatter(logging.Formatter('%(message)s'))
    handler = RotatingFileHandler(directory / 'worker.log', maxBytes=2*1024*1024, backupCount=2)
    handler.setFormatter(_Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
    recent = _Recent()
    for item in (handler, recent):
        logging.getLogger().addHandler(item)
        _handlers.append(item)
    for path in (directory / 'worker-events.jsonl', directory / 'worker.log'):
        path.chmod(0o600)
    # Native crashes bypass logging. Bound them by lifetime and retain four logs.
    native_path = directory / f'native-{identity}.log'
    _native = native_path.open('a')
    native_path.chmod(0o600)
    faulthandler.enable(_native, all_threads=True)
    for old in sorted(directory.glob('native-*.log'), key=lambda p:p.stat().st_mtime, reverse=True)[4:]:
        old.unlink(missing_ok=True)
    event('worker.starting')


def event(kind, *, severity='info', **details):
    value = {'at': datetime.now(timezone.utc).isoformat(), 'event': kind,
             'severity': severity, 'runtime_id': _identity, 'pid': os.getpid(),
             **{key:safe_text(item, 1500) if isinstance(item, str) else item for key,item in details.items()}}
    with _guard:
        _recent.append(value)
        if _events:
            _events.emit(logging.LogRecord('worker.events', logging.INFO, '', 0, json.dumps(value), (), None))
    return value


def exception(kind, exc):
    event(kind, severity='error', exception=type(exc).__name__, message=str(exc))
    logging.getLogger('lea.worker').error('%s\n%s', kind, ''.join(traceback.format_exception(exc)))


def snapshot():
    with _guard:
        values = list(_recent)
    return {'events': values, 'last_failure': next((v for v in reversed(values)
        if v.get('severity') in {'error', 'critical'}), None)}


def close():
    global _events, _native
    for handler in _handlers:
        logging.getLogger().removeHandler(handler)
        handler.close()
    _handlers.clear()
    if _events:
        _events.close()
        _events = None
    if _native:
        faulthandler.disable()
        _native.close()
        _native = None
