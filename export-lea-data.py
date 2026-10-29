"""Export current persisted Lea state without credentials or historical backups.

Manual use (with Lea's installed adapter Python):
    export-lea-data.py --root /path/to/Lea --reason manual-user-data-export --require-data

This file is self-contained and can be emailed as export-lea-data.py. Requires
Python 3.11+, git for proof history, and pdfminer.six when PDFs are present.
SQLite is exported logically, never as raw pages (which can retain deleted
secrets). Git history is exported through fast-export, never as opaque objects
or packfiles. Originals are read-only; only the final, sanitized ZIP is saved.
"""
from __future__ import annotations

import argparse
import base64
from collections import OrderedDict
from datetime import datetime, timezone
from functools import lru_cache
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
import tomllib
from urllib.parse import quote, quote_plus
from uuid import uuid4
import zipfile


REDACTED = "[REDACTED]"
SECRET_NAME = re.compile(
    r"(?:api|access|virtual)[_-]?key|authorization|authentication|(?:^|[_-])auth(?:$|[_-])|(?:access|refresh|auth|github)[_-]?token|"
    r"password|passwd|secret|credential|private[_-]?key|token$|^cookies?$|^set-cookie$", re.I)
TOKEN = re.compile(
    r"\b(?:sk-(?:ant-|proj-)?[A-Za-z0-9_-]{8,}|AIza[A-Za-z0-9_-]{20,}|"
    r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b")
ASSIGNMENT_PREFIX = re.compile(r'''(?im)(?<![\w.-])(["']?([\w.-]++)["']?\s*[:=]\s*)''')
ASSIGNMENT_VALUE = re.compile(r'''"(?:\\.|[^"\\])*"|'[^']*'|[^\s,;}]+''')
AUTH_HEADER = re.compile(r"(?i)\b(Bearer|Basic)\s+([A-Za-z0-9_+/=.-]+)")
URL_AUTH = re.compile(r"(?i)(https?://)([^\s/@]+:[^\s/@]+)@")
PRIVATE_KEY = re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.S)
SKIP_DIRS = {"node_modules", ".venv", ".lake", "__pycache__", ".pytest_cache", ".sv_scratch", "shutdown-archives",
             ".lea-runtime", ".lea-owned-processes"}
SECRET_FILES = re.compile(r"(?i)(?:^\.env(?:\..*)?$|^lea\.env$|\.(?:key|pem|p12|pfx|token)$|^\.(?:github_token|gemini_key|netrc|npmrc)$|credential|secret)")
SQLITE_HEADER = b"SQLite format 3\0"


@lru_cache(maxsize=8192)
def secret_field(name):
    return bool(SECRET_NAME.search(name)) and not name.endswith(("_name", "_from"))


def structured_json(value):
    # Most checkpoint leaves are plain words/IDs, not JSON documents. Avoid
    # constructing an exception for every one of millions of such values.
    if value.lstrip()[:1] not in {"{", "["}:
        return None
    try:
        parsed = json.loads(value)
    except (ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, (dict, list)) else None


def content_key(value):
    # Retain short repeated strings directly; retain only hashes of large
    # checkpoint/transcript strings so caches don't hold the whole data set.
    return value if len(value) < 256 else hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).digest()


def secret_assignments(value):
    # Consume each complete key once, then classify it. An unanchored greedy
    # prefix before a credential keyword retries at every character in a long
    # ordinary/base64 string and can spend minutes on a single transcript row.
    end = 0
    for match in ASSIGNMENT_PREFIX.finditer(value):
        if match.start() < end or not SECRET_NAME.search(match[2]):
            continue
        raw = ASSIGNMENT_VALUE.match(value, match.end())
        if raw is not None:
            end = raw.end()
            yield match.start(), end, match[1], raw[0]


def redact_assignments(value):
    parts, end = [], 0
    for start, stop, prefix, _ in secret_assignments(value):
        parts.extend((value[end:start], prefix, json.dumps(REDACTED)))
        end = stop
    if not parts:
        return value
    parts.append(value[end:])
    return "".join(parts)


class Progress:
    """CLI-only progress; never print user filenames, text, or credentials."""
    def __init__(self, heartbeat_seconds=10):
        self.started = time.monotonic()
        self.last_print = 0
        self.status = "Starting export"
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.heartbeat, args=(heartbeat_seconds,), daemon=True)

    def __call__(self, message, *, force=False):
        with self.lock:
            self.status = message
            now = time.monotonic()
            if force or now - self.last_print >= 1:
                print(f"[snapshot] [{int(now - self.started)}s] {message}", flush=True)
                self.last_print = now

    def heartbeat(self, interval):
        while not self.stopped.wait(interval):
            with self.lock:
                elapsed = int(time.monotonic() - self.started)
                print(f"[snapshot] [{elapsed}s] Still working: {self.status}", flush=True)

    def __enter__(self):
        self("Starting export; originals will not be changed", force=True)
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stopped.set()
        self.thread.join()


class Redactor:
    def __init__(self):
        self.secrets: set[str] = set()
        self._variants = None
        self._literal_pattern = None
        self._discovered = set()
        self._clean_cache = OrderedDict()
        self._clean_cache_size = 0
        self._disk_cache = None
        self._cache_generation = 0

    def disk_cache_path(self, key):
        if self._disk_cache is not None and key[0] == "value" and key[1] >= 4096:
            return self._disk_cache / f"{self._cache_generation}-{key[2].hex()}.txt"
        return None

    def cached(self, kind, value):
        key = (kind, len(value), content_key(value))
        entry = self._clean_cache.get(key)
        if entry is not None:
            self._clean_cache.move_to_end(key)
            return key, entry[0]
        disk = self.disk_cache_path(key)
        if disk is not None and disk.is_file():
            return key, disk.read_text(encoding="utf-8", errors="surrogatepass")
        return key, None

    def cache(self, key, value):
        # Bound memory to 64 MiB. A private, temporary disk cache retains large
        # SANITIZED strings across backup DBs even after memory-cache eviction.
        # Identical records still appear in every export; nothing is omitted.
        disk = self.disk_cache_path(key)
        if disk is not None and not disk.exists():
            disk.write_text(value, encoding="utf-8", errors="surrogatepass")
        size = len(value) * 4 + 128
        if size > 64 * 1024 * 1024:
            return
        old = self._clean_cache.pop(key, None)
        if old is not None:
            self._clean_cache_size -= old[1]
        while self._clean_cache and (self._clean_cache_size + size > 64 * 1024 * 1024 or len(self._clean_cache) >= 50000):
            _, (_, cost) = self._clean_cache.popitem(last=False)
            self._clean_cache_size -= cost
        self._clean_cache[key] = (value, size)
        self._clean_cache_size += size

    def remember(self, value):
        if isinstance(value, str) and value and value != REDACTED:
            if value not in self.secrets:
                self.secrets.add(value)
                self._variants = None
                self._literal_pattern = None
                self._clean_cache.clear()
                self._clean_cache_size = 0
                self._cache_generation += 1
        elif isinstance(value, (dict, list)):
            for item in value.values() if isinstance(value, dict) else value:
                self.remember(item)
        elif isinstance(value, bytes):
            self.remember(value.decode("utf-8", errors="replace"))

    def discover(self, value):
        if isinstance(value, dict):
            for key, item in value.items():
                self.discover(str(key))
                if secret_field(str(key)):
                    self.remember(item)
                else:
                    self.discover(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                self.discover(item)
        elif isinstance(value, str):
            key = content_key(value)
            if key in self._discovered:
                return
            if len(self._discovered) < 100000:
                self._discovered.add(key)
            parsed = structured_json(value)
            if parsed is not None:
                self.discover(parsed)
                # Every key/value has now been inspected, including nested
                # encoded content. Scanning its serialized copy repeats work
                # at every level of large nested recovery checkpoints.
                return
            decoded = base64_text(value)
            if decoded is not None:
                self.discover(decoded)
            for _, _, _, raw in secret_assignments(value):
                try:
                    raw = json.loads(raw)
                except ValueError:
                    raw = raw.strip("\"'")
                self.remember(raw)
            for match in AUTH_HEADER.finditer(value):
                self.remember(match[2])
            for match in URL_AUTH.finditer(value):
                self.remember(match[2])
                self.remember(match[2].split(":", 1)[1])
            for token in TOKEN.findall(value):
                self.remember(token)
        elif isinstance(value, bytes):
            collect_binary(self, value)

    def text(self, value: str) -> str:
        key, cached = self.cached("text", value)
        if cached is not None:
            return cached
        clean = self._text(value)
        self.cache(key, clean)
        return clean

    def _text(self, value: str) -> str:
        value = PRIVATE_KEY.sub(REDACTED, value)
        value = redact_assignments(value)
        value = AUTH_HEADER.sub(lambda m: m[1] + " " + REDACTED, value)
        value = URL_AUTH.sub(lambda m: m[1] + REDACTED + "@", value)
        value = TOKEN.sub(REDACTED, value)
        if self._variants is None:
            variants = set()
            for secret in self.secrets:
                variants.update((secret, quote(secret, safe=""), quote_plus(secret),
                                 json.dumps(secret)[1:-1], base64.b64encode(secret.encode()).decode()))
            self._variants = sorted(variants, key=len, reverse=True)
            if self._variants:
                self._literal_pattern = re.compile("|".join(re.escape(secret) for secret in self._variants))
        if self._literal_pattern is not None:
            value = self._literal_pattern.sub(lambda _: REDACTED, value)
        return value

    def value(self, value):
        if isinstance(value, dict):
            return {self.text(str(k)): REDACTED if secret_field(str(k)) else self.value(v)
                    for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.value(v) for v in value]
        if isinstance(value, str):
            key, cached = self.cached("value", value)
            if cached is not None:
                return cached
            parsed = structured_json(value)
            if parsed is not None:
                clean = json.dumps(self.value(parsed), ensure_ascii=False)
            else:
                decoded = base64_text(value)
                clean = (base64.b64encode(self.value(decoded).encode()).decode()
                         if decoded is not None else self.text(value))
            self.cache(key, clean)
            return clean
        if isinstance(value, bytes):
            return {"$base64": base64.b64encode(self.binary(value)).decode()}
        return value

    def binary(self, raw: bytes, depth=0) -> bytes:
        # Uploaded DOCX/ZIP and compressed logs must be sanitized *inside* their
        # compression too. Recursion/size limits reject unsafe archive inputs.
        if depth > 8:
            raise ValueError("Nested archive exceeds sanitization depth")
        if raw.startswith(b"%PDF-"):
            records = list(pdf_records(raw))
            if any(self.affects(record) for record in records):
                return json.dumps({"format": "lea-redacted-pdf-v1", "objects": self.value(records)}, ensure_ascii=False).encode()
            return raw
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            encoding = "utf-16-le" if raw.startswith(b"\xff\xfe") else "utf-16-be"
            return raw[:2] + self.text(raw[2:].decode(encoding)).encode(encoding)
        if raw.startswith(SQLITE_HEADER):
            with sqlite3.connect(":memory:") as conn:
                conn.deserialize(raw)
                return b"".join((json.dumps(self.value(row), ensure_ascii=False, default=str) + "\n").encode()
                                for row in connection_rows(conn))
        if raw.startswith(b"PK\x03\x04"):
            output = io.BytesIO()
            with zipfile.ZipFile(io.BytesIO(raw)) as source, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
                if sum(i.file_size for i in source.infolist()) > 256 * 1024 * 1024:
                    raise ValueError("Nested archive exceeds sanitization size")
                for info in source.infolist():
                    if info.flag_bits & 1:
                        raise ValueError("Encrypted archive cannot be sanitized")
                    if SECRET_FILES.search(Path(info.filename).name):
                        continue
                    original = source.read(info)
                    clean = self.binary(original, depth + 1)
                    target.writestr(export_name(self.text(info.filename), original, clean), clean)
            return output.getvalue()
        if raw.startswith(b"\x1f\x8b"):
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as source:
                expanded = source.read(256 * 1024 * 1024 + 1)
            if len(expanded) > 256 * 1024 * 1024:
                raise ValueError("Compressed file exceeds sanitization size")
            return gzip.compress(self.binary(expanded, depth + 1), mtime=0)
        # latin-1 is a lossless byte mapping: arbitrary uploaded files are kept,
        # and literal credentials in their metadata are scrubbed as well.
        try:
            text = raw.decode("utf-8")
        except UnicodeError:
            text = None
        if text is not None:
            return self.value(text).encode()
        for secret in self.secrets:
            raw = raw.replace(secret.encode(), REDACTED.encode())
        return self.text(raw.decode("latin-1")).encode("latin-1")

    def affects(self, value):
        if isinstance(value, dict):
            return any(SECRET_NAME.search(str(k)) or self.affects(v) for k, v in value.items())
        if isinstance(value, (list, tuple)):
            return any(self.affects(v) for v in value)
        if isinstance(value, str):
            return self.value(value) != value
        if isinstance(value, bytes):
            return self.binary(value) != value
        return False


def export_name(name, original, clean):
    if original.startswith(b"%PDF-") and not clean.startswith(b"%PDF-"):
        return name + ".redacted.json"
    return name


def pdf_records(raw):
    # pdfminer is already an application dependency. Inspect decoded streams
    # and metadata, including objects compressed with Flate/ASCII85 filters.
    # A credential-bearing PDF is exported as sanitized logical objects so no
    # original compressed stream can smuggle the credential into the archive.
    from pdfminer.pdfdocument import PDFDocument
    from pdfminer.pdfparser import PDFParser
    from pdfminer.pdftypes import PDFObjRef, PDFStream
    from pdfminer.psparser import PSLiteral, PSKeyword

    def record(value):
        if isinstance(value, PDFObjRef):
            return {"$ref": value.objid}
        if isinstance(value, PDFStream):
            data = value.get_data()
            if len(data) > 256 * 1024 * 1024:
                raise ValueError("PDF stream exceeds sanitization size")
            return {"attributes": record(value.attrs), "decoded_stream": data}
        if isinstance(value, (PSLiteral, PSKeyword)):
            return value.name.decode("latin-1") if isinstance(value.name, bytes) else value.name
        if isinstance(value, dict):
            return {str(k): record(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [record(v) for v in value]
        return value

    document = PDFDocument(PDFParser(io.BytesIO(raw)))
    seen = set()
    yield {"metadata": record(document.info)}
    for xref in document.xrefs:
        for identity in xref.get_objids():
            if identity not in seen:
                seen.add(identity)
                yield {"id": identity, "object": record(document.getobj(identity))}


def base64_text(value):
    if len(value) < 12 or not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", value):
        return None
    try:
        text = base64.b64decode(value, validate=True).decode()
    except (ValueError, UnicodeError):
        return None
    return text if text and all(c.isprintable() or c.isspace() for c in text) else None


def dotenv_values(file):
    values = {}
    if not file.exists():
        return values
    for line in file.read_text().splitlines():
        key, separator, value = line.strip().partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        if value.startswith('"') and value.endswith('"'):
            try:
                value = json.loads(value)
            except ValueError:
                value = value[1:-1]
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        values[key] = value
    return values


def database_rows(file: Path):
    # A single read transaction pins one consistent SQLite snapshot, including
    # committed WAL data. No raw database/journal/free pages enter the ZIP.
    with sqlite3.connect(file.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.execute("begin")
        yield from connection_rows(conn)


def connection_rows(conn):
    schema = conn.execute("select type,name,tbl_name,sql from sqlite_master where sql is not null order by name").fetchall()
    yield {"format": "lea-sqlite-export-v1", "schema": schema}
    for kind, name, _, _ in schema:
        if kind != "table":
            continue
        cursor = conn.execute('select * from "' + name.replace('"', '""') + '"')
        columns = [c[0] for c in cursor.description]
        for row in cursor:
            yield {"table": name, "row": dict(zip(columns, row))}


def git_export(repo: Path):
    # fast-export exposes historical blobs even after they were deleted or
    # packed. Recompute every data length after redaction to keep it importable.
    command = ["git", "-C", str(repo), "fast-export", "--all", "--signed-tags=strip", "--tag-of-filtered-object=drop"]
    with tempfile.TemporaryFile() as errors:
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors) as child:
            try:
                while line := child.stdout.readline():
                    if line.startswith(b"data "):
                        size = int(line[5:])
                        data = child.stdout.read(size)
                        if len(data) != size:
                            raise ValueError("Incomplete Git export")
                        yield True, data
                    else:
                        yield False, line
                if child.wait() != 0:
                    raise ValueError("Git history export failed")
            except BaseException:
                child.kill()
                child.wait()
                raise


def locations(root: Path, env):
    root = root.resolve()
    app = root / "apps/lea-standalone" if (root / "apps/lea-standalone/adapter").is_dir() else root
    resolve = lambda value, base=root: (base / value).resolve()
    config = resolve(env.get("LEA_CONFIG_FILE", str(app / "config/lea.local.toml")))
    try:
        settings = tomllib.loads(config.read_text()) if config.exists() else {}
    except tomllib.TOMLDecodeError:
        settings = {}
    prover = (resolve(env["LEA_ROOT"]) if env.get("LEA_ROOT")
              else resolve(settings.get("lea_root") or str(app / "prover"), app))
    data = resolve(env.get("LEA_SHARED_DATA_DIR", str(app / "data")))
    if env.get("LEA_DATABASE_FILE"):
        data = resolve(env["LEA_DATABASE_FILE"]).parent
    companion = resolve(env.get("LEA_COMPANION_DATA_DIR", str(root / "apps/overleaf-extension/.overleaf-lean-stub")))
    sources = [("data", data), ("config", config.parent), ("workspace", prover / "workspace"),
               ("overleaf-state", companion)]
    if env.get("LEA_COMPANION_DATA_DIR") and not Path(env["LEA_COMPANION_DATA_DIR"]).is_absolute():
        # npm starts the companion with its workspace as cwd. Also retain the
        # root-relative interpretation for installations started from the root.
        sources.append(("companion-cwd-state", resolve(env["LEA_COMPANION_DATA_DIR"], root / "apps/overleaf-extension")))
    # The adapter resolves lea_root from TOML relative to the standalone app;
    # companion/startup settings can also point at a different prover. Preserve
    # both when they disagree, rather than silently losing one workspace.
    adapter_prover = resolve(settings.get("lea_root") or str(app / "prover"), app)
    if adapter_prover != prover:
        sources.append(("adapter-workspace", adapter_prover / "workspace"))
    if app != root:
        # Docker bind mounts also live here on the host. Include these if this
        # checkout has been used for both local and Docker installations.
        sources.extend([(label, app / directory) for label, directory in (
            ("docker-proofs", "proofs"), ("docker-projects", "projects"),
            ("docker-overleaf-state", "overleaf-state"))])
        sources.append(("legacy-data", root / "apps/lea-ui/data"))
    return data / "shutdown-archives", sources, config, root / ".env"


def inventory(sources, output, *, include_backups=False, active_database="lea-interface.sqlite3"):
    files, repos, omitted, credential_sources = [], [], [], []
    for label, source in sources:
        if not include_backups and label == "legacy-data":
            if source.exists():
                omitted.append((label + "/", "legacy installation data"))
            continue
        if not source.exists():
            continue
        for directory, dirs, names in os.walk(source, followlinks=False):
            directory = Path(directory)
            relative = Path(label) / directory.relative_to(source)
            if (directory / ".git").exists() and not (directory / ".git").is_symlink():
                repos.append((directory, relative))
            for name in list(dirs):
                entry = directory / name
                if not include_backups and name.lower() in {"backup", "backups", ".backups"}:
                    omitted.append(((relative / name).as_posix() + "/", "historical backup directory"))
                    dirs.remove(name)
                elif entry.is_symlink() or name in SKIP_DIRS or name == ".git" or entry.resolve() == output.resolve():
                    dirs.remove(name)
            for name in names:
                entry = directory / name
                archive_name = (relative / name).as_posix()
                historical_file = (name.lower().endswith((".bak", ".backup", "~"))
                    or (label == "data" and directory == source
                        and name != active_database
                        and re.fullmatch(r"lea-interface(?:[-.].*)?\.(?:sqlite3|sqlite|db)(?:\.(?:gz|zip))?", name, re.I)))
                if not include_backups and historical_file:
                    omitted.append((archive_name, "historical backup file"))
                elif entry.is_symlink() or not stat.S_ISREG(entry.stat().st_mode):
                    omitted.append((archive_name, "symlink or special file"))
                elif SECRET_FILES.search(name) or "credential-intake" in entry.parts:
                    credential_sources.append(entry)
                    omitted.append((archive_name, "credential file"))
                elif name == ".git" or name.endswith(("-wal", "-shm", "-journal", ".lock")):
                    omitted.append((archive_name, "credential or transient file"))
                else:
                    files.append((entry, archive_name))
    return files, repos, omitted, credential_sources


def is_database(file):
    with file.open("rb") as handle:
        return handle.read(16) == SQLITE_HEADER


def collect_binary(redactor, raw, depth=0):
    if depth > 8:
        raise ValueError("Nested archive exceeds sanitization depth")
    if raw.startswith(SQLITE_HEADER):
        with sqlite3.connect(":memory:") as conn:
            conn.deserialize(raw)
            for row in connection_rows(conn):
                redactor.discover(row)
    elif raw.startswith(b"%PDF-"):
        for row in pdf_records(raw):
            redactor.discover(row)
    elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        redactor.discover(raw.decode("utf-16"))
    elif raw.startswith(b"PK\x03\x04"):
        with zipfile.ZipFile(io.BytesIO(raw)) as source:
            if sum(i.file_size for i in source.infolist()) > 256 * 1024 * 1024:
                raise ValueError("Nested archive exceeds sanitization size")
            for info in source.infolist():
                collect_binary(redactor, source.read(info), depth + 1)
    elif raw.startswith(b"\x1f\x8b"):
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as source:
            expanded = source.read(256 * 1024 * 1024 + 1)
        if len(expanded) > 256 * 1024 * 1024:
            raise ValueError("Compressed file exceeds sanitization size")
        collect_binary(redactor, expanded, depth + 1)
    else:
        redactor.discover(raw.decode("utf-8", errors="replace"))


def collect_file(redactor, file, progress=None):
    if is_database(file):
        for count, row in enumerate(database_rows(file)):
            redactor.discover(row)
            if progress and count and count % 1000 == 0:
                progress(f"Scanning database: {count:,} records checked")
        return
    if file.suffix.lower() == ".pdf":
        collect_binary(redactor, file.read_bytes())
        return
    try:
        text = file.read_text()
    except UnicodeError:
        collect_binary(redactor, file.read_bytes())
        return
    if file.suffix == ".toml":
        try:
            redactor.discover(tomllib.loads(text))
        except tomllib.TOMLDecodeError:
            pass  # still scan historical or incomplete TOML as text
    redactor.discover(text)


def has_user_data(files):
    starter_files = {"lake-manifest.json", "lakefile.lean", "lakefile.toml", "lea.md", "lean-toolchain", ".gitignore"}
    for _, name in files:
        parts = Path(name).parts
        if parts[0] == "config" or parts[-1] == ".gitkeep":
            continue
        if parts[0] in {"workspace", "adapter-workspace"}:
            if parts[1].startswith(".lea-") or (len(parts) == 2 and parts[1] in starter_files):
                continue
        return True
    return False


def create_snapshot(root: Path, *, env=None, reason="shutdown", require_data=False, progress=None, include_backups=False) -> Path:
    report = progress or (lambda message, **kwargs: None)
    report("Locating saved data and listing files", force=True)
    root = root.resolve()
    # Direct/manual invocation does not go through the Node supervisor, which
    # normally loads .env. Parse it as data; never execute a user's .env file.
    root_settings = dotenv_values(root / ".env")
    env = {**root_settings, **dict(os.environ if env is None else env)}
    output, sources, config, dot_env = locations(root, env)
    active_database = Path(env.get("LEA_DATABASE_FILE") or "lea-interface.sqlite3").name
    files, repos, omitted, credential_sources = inventory(sources, output, include_backups=include_backups,
                                                       active_database=active_database)
    if require_data and not has_user_data(files):
        raise ValueError("No persisted user data found")
    scope = "including historical backups" if include_backups else "current data; historical backups excluded"
    report(f"Found {len(files):,} files and {len(repos):,} proof repositories ({scope})", force=True)
    redactor = Redactor()
    redactor.discover(env)
    redactor.discover(root_settings)
    # Scan before writing anything: a secret found in a later settings record
    # also needs to be removed from an earlier transcript or historical blob.
    scan_files = list(dict.fromkeys([config, dot_env, *credential_sources, *(f for f, _ in files)]))
    for index, file in enumerate(scan_files, 1):
        if file.is_file():
            report(f"Scanning file {index:,}/{len(scan_files):,} for credentials")
            file_progress = (lambda message: report(f"File {index:,}/{len(scan_files):,}: {message}")) if progress else None
            collect_file(redactor, file, progress=file_progress)
    for file in credential_sources:
        # A .token/.key file may consist solely of an unlabeled value. Never
        # export it, and also remove copies of that value from other records.
        if file.suffix in {".token", ".key"} or file.name in {".github_token", ".gemini_key"}:
            redactor.remember(file.read_text(errors="replace").strip())
    for index, (repo, _) in enumerate(repos, 1):
        report(f"Scanning Git history {index:,}/{len(repos):,}", force=True)
        blobs = 0
        for is_data, raw in git_export(repo):
            collect_binary(redactor, raw)
            if is_data:
                blobs += 1
                report(f"Scanning Git history {index:,}/{len(repos):,}: {blobs:,} data records checked")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    created = datetime.now(timezone.utc)
    identity = created.strftime("%Y-%m-%dT%H-%M-%S.%fZ") + "-" + uuid4().hex[:12]
    prefix = "lea-snapshot" if include_backups else "lea-current-data"
    destination = output / f"{prefix}-{identity}.zip"
    fd, temporary = tempfile.mkstemp(prefix=".snapshot-", suffix=".partial", dir=output)
    os.close(fd)
    manifest = {"format_version": 1, "created_at": created.isoformat(), "reason": reason,
                "snapshot": "full" if include_backups else "current",
                "historical_backups_included": include_backups,
                "files": [], "omitted": omitted,
                "sources": [{"label": label, "present": source.is_dir(),
                             "included": include_backups or label != "legacy-data"} for label, source in sources],
                "notes": ["Credentials are redacted; originals are never changed.",
                          "SQLite files are logical JSONL exports, without deleted pages or journals.",
                          "Git history uses sanitized fast-export streams; caches and symlinks are excluded.",
                          "Credential-bearing PDFs use redacted decoded-object JSON exports.",
                          "Browser-only state is included only if manually saved inside an exported data directory.",
                          "Provider-side records and data deleted before this export are not retrieved."]}
    try:
        report("Building sanitized ZIP", force=True)
        with tempfile.TemporaryDirectory(prefix="lea-sanitized-export-cache-") as cache_directory, \
                zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            redactor._disk_cache = Path(cache_directory)
            for index, (file, name) in enumerate(files, 1):
                report(f"Writing file {index:,}/{len(files):,}")
                name = redactor.text(name)
                if is_database(file):
                    name += ".jsonl"
                    with archive.open(name, "w") as target:
                        for count, row in enumerate(database_rows(file)):
                            target.write((json.dumps(redactor.value(row), ensure_ascii=False, default=str) + "\n").encode())
                            if progress and count and count % 1000 == 0:
                                report(f"Writing file {index:,}/{len(files):,}, database: {count:,} records exported")
                elif file.suffix == ".toml":
                    try:
                        data = tomllib.loads(file.read_text())
                    except tomllib.TOMLDecodeError:
                        archive.writestr(name, redactor.binary(file.read_bytes()))
                    else:
                        archive.writestr(name + ".json", json.dumps(redactor.value(data), default=str, indent=2))
                        name += ".json"
                else:
                    original = file.read_bytes()
                    clean = redactor.binary(original)
                    name = export_name(name, original, clean)
                    archive.writestr(name, clean)
                manifest["files"].append(name)
            for index, (repo, relative) in enumerate(repos, 1):
                report(f"Writing Git history {index:,}/{len(repos):,}", force=True)
                name = redactor.text((relative / "history.fast-export").as_posix())
                with archive.open(name, "w") as target:
                    blobs = 0
                    for is_data, raw in git_export(repo):
                        clean = redactor.binary(raw)
                        if is_data:
                            target.write(f"data {len(clean)}\n".encode())
                            blobs += 1
                            report(f"Writing Git history {index:,}/{len(repos):,}: {blobs:,} data records exported")
                        target.write(clean)
                manifest["files"].append(name)
            runtime_settings = {k: env.get(k, v) for k, v in root_settings.items()}
            runtime_settings.update({k: v for k, v in env.items()
                if k.startswith(("LEA_", "OVERLEAF_", "PORTKEY_")) or k.endswith(("_BASE_URL", "_API_BASE"))})
            archive.writestr("runtime-settings.json", json.dumps(redactor.value(runtime_settings), indent=2))
            archive.writestr("manifest.json", json.dumps(redactor.value(manifest), indent=2))
        report("Publishing completed ZIP", force=True)
        with open(temporary, "rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        return destination
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--reason", default="shutdown")
    parser.add_argument("--require-data", action="store_true",
                        help="Fail instead of creating an export without persisted user data (recommended for manual exports)")
    parser.add_argument("--quiet", action="store_true", help="Hide progress; print only the final result")
    parser.add_argument("--include-backups", action="store_true",
                        help="Also export historical backup folders/files and legacy installation data (excluded by default)")
    args = parser.parse_args()
    try:
        if args.require_data and not ((args.root / "apps/lea-standalone/adapter").is_dir()
                                      or (args.root / "adapter").is_dir()):
            parser.error("--root must name the existing Lea repository, or /app inside its Docker container")
        if args.quiet:
            destination = create_snapshot(args.root, reason=args.reason, require_data=args.require_data,
                                          include_backups=args.include_backups)
        else:
            with Progress() as progress:
                destination = create_snapshot(args.root, reason=args.reason, require_data=args.require_data, progress=progress,
                                              include_backups=args.include_backups)
    except KeyboardInterrupt:
        print("[snapshot] Export cancelled; originals were not changed.", flush=True)
        return 130
    except Exception as error:
        # Exceptions can contain credential-bearing filenames, SQL, or Git URLs.
        print(f"[snapshot] Export failed ({type(error).__name__}); no archive was published.", flush=True)
        if args.require_data:
            print("[snapshot] Check the existing repository path, data directory overrides, read permissions, and the adapter Python environment. Do not clone a fresh repository to export an existing installation.", flush=True)
        return 1
    print(f"[snapshot] Saved {destination}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
