"""Shared helpers: paths, hashing, canonical JSON, SQLite store, status vocabulary, exit codes."""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import sqlite3
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "ROOT", "path", "sha256_bytes", "sha256_file", "cjson", "jhash", "utcnow", "load_yaml", "DB", "Status",
    "ExitCode", "FormbenchError", "run_cmd", "write_json", "read_json", "write_text",
]


def _find_root() -> Path:
    env = os.environ.get("FORMBENCH_ROOT")
    if env:
        return Path(env).resolve()
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "AGENTS.md").exists() and (p / "configs").is_dir():
            return p
    return Path.cwd()


ROOT = _find_root()


def path(*parts: str) -> Path:
    """Project-relative path (honours FORMBENCH_ROOT so tests can run in a sandbox)."""
    root = Path(os.environ["FORMBENCH_ROOT"]).resolve() if os.environ.get("FORMBENCH_ROOT") else ROOT
    return root.joinpath(*parts)


def obs_name() -> str:
    """Observation directory under data/; FORMBENCH_OBS selects the experiment directory."""
    return os.environ.get("FORMBENCH_OBS", "source_documents")


class Status:
    """Controlled status vocabulary (AGENTS.md §1, §12). Never use 'deprecated' for unimplemented features."""

    OK = "ok"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"
    UNSUPPORTED_FEATURE = "unsupported_feature"
    UNSUPPORTED_OPERATION = "unsupported_operation"
    BLOCKED = "blocked"
    BLOCKED_BY_ENVIRONMENT = "blocked_by_environment"
    RIGHTS_RESTRICTED = "rights_restricted"
    SOLVER_UNPROVEN = "solver_unproven"
    TIMEOUT = "timeout"
    RESOURCE_EXHAUSTED = "resource_exhausted"      # killed by the memory guard; machine dependent like TIMEOUT
    KNOWN_ISSUE = "known_issue"
    PARTIAL = "partial"
    SKIPPED_POLICY = "skipped_policy"
    QUARANTINED = "quarantined"


class ExitCode:
    """Machine-readable CLI exit codes."""

    OK = 0
    FAILURE = 1
    USAGE = 2
    VALIDATION_FAILED = 3
    BLOCKED_BY_ENVIRONMENT = 4
    POLICY_BLOCKED = 5
    PARTIAL = 6
    NOT_REPRODUCIBLE = 7


class FormbenchError(Exception):
    def __init__(self, message: str, code: str = Status.FAILED, exit_code: int = ExitCode.FAILURE):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(p: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def cjson(obj: Any) -> str:
    """Canonical JSON: sorted keys, no whitespace variance, UTF-8 preserved."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default)


def _json_default(o: Any) -> Any:
    if isinstance(o, Path):
        return str(o)
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    if hasattr(o, "to_json"):
        return o.to_json()
    if hasattr(o, "__dataclass_fields__"):
        import dataclasses
        return dataclasses.asdict(o)
    if hasattr(o, "item"):
        return o.item()
    raise TypeError(f"not JSON serialisable: {type(o)}")


def jhash(obj: Any) -> str:
    return sha256_bytes(cjson(obj).encode("utf-8"))


def utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_yaml(p: str | Path) -> Any:
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f)


def write_json(p: str | Path, obj: Any, indent: int | None = 1) -> None:
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=indent, default=_json_default) + "\n",
                   encoding="utf-8")
    tmp.replace(p)


def read_json(p: str | Path) -> Any:
    return json.loads(Path(p).read_text(encoding="utf-8"))


def write_text(p: str | Path, text: str) -> None:
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def run_cmd(cmd: list[str], timeout: float = 120, env: dict | None = None, cwd: str | None = None) -> tuple[int, str, str]:
    """Run an external tool without a shell. Returns (rc, stdout, stderr); rc=-9 on timeout, -127 if missing.

    The tool runs in its own session with core dumps disabled; on timeout the whole process group is killed
    (``soffice`` is a launcher: killing only the direct child would leave ``soffice.bin`` / a JVM behind)."""
    from .resources import child_limits
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=cwd, start_new_session=True, preexec_fn=child_limits)
    except FileNotFoundError:
        return -127, "", "not found"
    try:
        out, err = p.communicate(timeout=timeout)
        return p.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return -9, "", "timeout"
    finally:
        try:
            os.killpg(p.pid, 9)                       # also reaps helpers a finished launcher left running
        except (ProcessLookupError, PermissionError):
            pass
        if p.poll() is None:
            p.wait()


SCHEMA = """
CREATE TABLE IF NOT EXISTS search_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT, engine TEXT, query TEXT, rank INTEGER, url TEXT, title TEXT,
  snippet TEXT, discovered_at TEXT, status TEXT, reason TEXT, is_duplicate INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS query_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT, engine TEXT, query TEXT, ts TEXT, status TEXT, n_results INTEGER,
  reason TEXT);
CREATE TABLE IF NOT EXISTS candidates(
  url TEXT PRIMARY KEY, kind TEXT, source_domain TEXT, crawl_query TEXT, provider TEXT, discovered_at TEXT,
  parent_url TEXT, link_text TEXT, guessed_format TEXT, depth INTEGER DEFAULT 0, policy_status TEXT,
  policy_reason TEXT, fetch_status TEXT, document_id TEXT, extra TEXT);
CREATE TABLE IF NOT EXISTS policy_cache(
  domain TEXT PRIMARY KEY, robots_status TEXT, robots_txt TEXT, fetched_at TEXT, http_status INTEGER, notes TEXT);
CREATE TABLE IF NOT EXISTS http_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, url TEXT, method TEXT, status INTEGER, bytes INTEGER,
  elapsed_ms INTEGER, etag TEXT, last_modified TEXT, from_cache INTEGER, error TEXT);
CREATE TABLE IF NOT EXISTS http_cache(
  url TEXT PRIMARY KEY, etag TEXT, last_modified TEXT, sha256 TEXT, content_type TEXT, fetched_at TEXT, status INTEGER,
  headers TEXT);
CREATE TABLE IF NOT EXISTS documents(document_id TEXT PRIMARY KEY, sha256 TEXT, record TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS takedown(pattern TEXT PRIMARY KEY, reason TEXT, added_at TEXT);
CREATE TABLE IF NOT EXISTS domain_state(domain TEXT PRIMARY KEY, blocked_until TEXT, stop_reason TEXT, n_requests INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS queue(stage TEXT, key TEXT, status TEXT, attempts INTEGER DEFAULT 0, updated_at TEXT,
  error TEXT, PRIMARY KEY(stage, key));
CREATE TABLE IF NOT EXISTS pairs(pair_id TEXT PRIMARY KEY, template_id TEXT, completed_id TEXT, pair_type TEXT,
  evidence TEXT, confidence REAL, eval_eligible INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS review_queue(item_id TEXT PRIMARY KEY, kind TEXT, payload TEXT, status TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS idx_docs_sha ON documents(sha256);
"""


class DB:
    """Thin SQLite wrapper used as metadata store and resumable work queue."""

    def __init__(self, db_path: str | Path | None = None):
        self.path = Path(db_path) if db_path else path("data", "formbench.sqlite")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=60)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=60000")
        # NORMAL avoids an fsync per statement (the store is a rebuildable work queue); OFF is used for throw-away test databases
        self.conn.execute(f"PRAGMA synchronous={os.environ.get('FORMBENCH_SQLITE_SYNCHRONOUS', 'NORMAL')}")
        if os.environ.get("FORMBENCH_SQLITE_WAL") == "1":        # opt-in: WAL needs shared memory, which hangs on some filesystems
            try:
                self.conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                pass
        if self.conn.execute("SELECT count(*) FROM sqlite_master WHERE name='review_queue'").fetchone()[0] == 0:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def x(self, sql: str, args: Iterable = ()) -> sqlite3.Cursor:
        # many worker processes share this file: a write that waited busy_timeout (60 s) for the lock is retried
        # with back-off instead of killing a long stage (2026-10-15: validate died on 'database is locked')
        import time as _t
        for attempt in range(8):
            try:
                cur = self.conn.execute(sql, tuple(args))
                self.conn.commit()
                return cur
            except sqlite3.OperationalError as e:
                if "locked" not in str(e) and "busy" not in str(e) or attempt == 7:
                    raise
                try:
                    self.conn.rollback()
                except sqlite3.Error:
                    pass
                _t.sleep(min(30.0, 2.0 * (2 ** attempt)))
        raise sqlite3.OperationalError("database is locked")

    def q(self, sql: str, args: Iterable = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(args)).fetchall()

    def one(self, sql: str, args: Iterable = ()) -> sqlite3.Row | None:
        r = self.conn.execute(sql, tuple(args)).fetchone()
        return r

    # --- queue (resumable stages) ---
    def q_put(self, stage: str, key: str) -> None:
        self.x("INSERT OR IGNORE INTO queue(stage,key,status,updated_at) VALUES(?,?,?,?)", (stage, key, "pending", utcnow()))

    def q_done(self, stage: str, key: str, status: str = "done", error: str | None = None) -> None:
        self.x("INSERT INTO queue(stage,key,status,attempts,updated_at,error) VALUES(?,?,?,1,?,?) "
               "ON CONFLICT(stage,key) DO UPDATE SET status=excluded.status, attempts=attempts+1, "
               "updated_at=excluded.updated_at, error=excluded.error", (stage, key, status, utcnow(), error))

    def q_status(self, stage: str, key: str) -> str | None:
        r = self.one("SELECT status FROM queue WHERE stage=? AND key=?", (stage, key))
        return r["status"] if r else None

    # --- documents ---
    def put_doc(self, rec: dict) -> None:
        self.x("INSERT INTO documents(document_id,sha256,record,updated_at) VALUES(?,?,?,?) "
               "ON CONFLICT(document_id) DO UPDATE SET sha256=excluded.sha256, record=excluded.record, "
               "updated_at=excluded.updated_at", (rec["document_id"], rec.get("sha256"), cjson(rec), utcnow()))

    def get_doc(self, document_id: str) -> dict | None:
        r = self.one("SELECT record FROM documents WHERE document_id=?", (document_id,))
        return json.loads(r["record"]) if r else None

    def docs(self) -> list[dict]:
        return [json.loads(r["record"]) for r in self.q("SELECT record FROM documents ORDER BY document_id")]

    def close(self) -> None:
        self.conn.close()


def network_up(timeout: float = 3.0) -> bool:
    """Is the internet reachable at all (not just one site)? TCP to two anycast resolvers or one DNS lookup.
    Used to tell 'this site failed' from 'the VM lost its network' (2026-10-15 03:2x)."""
    import socket
    import threading
    from . import dnsfallback
    dnsfallback.install()
    reach = False
    for host, port in (("1.1.1.1", 443), ("8.8.8.8", 53), ("9.9.9.9", 443)):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                reach = True
                break
        except OSError:
            continue
    if not reach:
        return False
    # the system resolver must work too (2026-10-15: IP reachable, WSL DNS proxy dead -> every fetch failed)
    ok = []
    def resolve():
        for name in ("www.google.com", "www.wikipedia.org"):
            try:
                socket.getaddrinfo(name, 443)
                ok.append(True)
                return
            except OSError:
                continue
    t = threading.Thread(target=resolve, daemon=True)
    t.start()
    t.join(timeout * 5)
    return bool(ok)
