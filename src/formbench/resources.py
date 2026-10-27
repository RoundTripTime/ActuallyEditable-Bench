"""Host resource guards.

Three independent layers keep a runaway stage from taking the whole machine down (host OOM of 2026-10-14):

1. ``ensure_memory_scope``: the CLI re-executes itself inside a transient systemd user scope with ``MemoryMax`` and
   no swap, so the kernel kills *our* processes (SIGKILL, no core dump) instead of thrashing the host.
2. ``run_jobs``: every job runs in its own freshly spawned process group. A watchdog kills a job whose resident
   memory (job + its subprocesses) exceeds the per-job cap and reports it; under host memory pressure the youngest
   job is re-queued instead of being counted as a failure. Memory is returned to the OS after every job.
3. ``child_limits``: external tools get ``RLIMIT_CORE = 1``. WSL pipes every core dump to ``wsl-capture-crash``
   (1.2 GB per aborting soffice.bin); the kernel skips a piped dump only when the limit is exactly 1.

Nothing here decides a score. A job killed for memory is reported as ``resource_exhausted`` by the caller; like a
time-out it depends on the machine and is listed separately by ``formbench reproduce``.
"""
from __future__ import annotations

import collections
import multiprocessing as mp
import multiprocessing.connection as mpc
import os
import pickle
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

from .util import load_yaml, path

DEFAULTS = {"guard": "auto", "scope_max_fraction": 0.6, "reserve_mb": 4096, "job_mem_mb": 3072, "job_expect_mb": 1024, "poll_s": 0.5,
            "max_workers": 8}
ENV_IN_SCOPE = "FORMBENCH_MEM_SCOPE_MB"
LIGHT_COMMANDS = {"doctor", "capabilities", "report", "results", "--help", "-h"}
# commands that render, convert, reconstruct or search plans: they may use many GB, so without the kernel-enforced
# scope they do not start (guard 'auto'); collection / corpus bookkeeping falls back to the in-process watchdog
HEAVY_COMMANDS = {"benchmark", "reconstruct", "render", "reproduce", "experiment", "pilot"}
EVENT_LOG = ("data", "logs", "resources.jsonl")


def log_event(kind: str, **fields) -> None:
    """One JSON line per CLI start / guard decision / watchdog kill in data/logs/resources.jsonl. The system journal can
    be lost when the VM restarts (2026-10-15: it was rotated away by a WSL boot loop), this file is ours."""
    import json
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "kind": kind, "pid": os.getpid(), "boot_id": _boot_id(),
           "mem_available_mb": mem_available_mb(), **fields}
    try:
        f = path(*EVENT_LOG)
        f.parent.mkdir(parents=True, exist_ok=True)
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def start_self_watchdog(limit_mb: int, poll_s: float = 1.0, log=None) -> None:
    """Fallback when no memory scope can be created: a daemon thread watches this process tree and ends it (SIGKILL to
    the children, then exit 137) before it reaches ``limit_mb``. Not as strict as the kernel limit (it samples), but it
    keeps a runaway stage from taking the host down."""
    import threading

    def watch():
        me = os.getpid()
        while True:
            rss = tree_rss_mb(me)
            if rss > limit_mb:
                log_event("self_watchdog_kill", rss_mb=rss, limit_mb=limit_mb, argv=sys.argv[1:])
                sys.stderr.write(f"formbench: process tree uses {rss} MB > {limit_mb} MB without a memory scope; stopping (resource_exhausted)\n")
                try:
                    import psutil
                    for c in psutil.Process(me).children(recursive=True):
                        try:
                            c.kill()
                        except Exception:      # noqa: BLE001
                            pass
                except Exception:      # noqa: BLE001
                    pass
                os._exit(137)
            time.sleep(poll_s)
    threading.Thread(target=watch, name="formbench-mem-watchdog", daemon=True).start()


def settings() -> dict:
    """configs/resources.yaml (not part of the metric contract) with environment overrides."""
    s = dict(DEFAULTS)
    p = path("configs", "resources.yaml")
    if p.exists():
        s.update((load_yaml(p) or {}).get("memory", {}))
    for env, key, cast in (("FORMBENCH_MEM_GUARD", "guard", str), ("FORMBENCH_MEM_MAX_MB", "scope_max_mb", int),
                           ("FORMBENCH_JOB_MEM_MB", "job_mem_mb", int), ("FORMBENCH_MAX_WORKERS", "max_workers", int)):
        if os.environ.get(env):
            s[key] = cast(os.environ[env])
    return s


# ----------------------------------------------------------------------------- measurements
def _meminfo() -> dict:
    out = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":", 1)
            out[k] = int(v.split()[0]) // 1024
    except OSError:
        pass
    return out


def mem_total_mb() -> int:
    return _meminfo().get("MemTotal", 0)


def mem_available_mb() -> int:
    """Host MemAvailable in MB (0 if unknown: callers then skip pressure handling)."""
    return _meminfo().get("MemAvailable", 0)


def cgroup_limit_mb() -> int | None:
    """memory.max of the cgroup this process lives in (None if unlimited or not cgroup v2)."""
    try:
        rel = Path("/proc/self/cgroup").read_text().strip().split("::", 1)[1]
        v = Path("/sys/fs/cgroup" + rel, "memory.max").read_text().strip()
        return None if v == "max" else int(v) // (1 << 20)
    except (OSError, IndexError, ValueError):
        return None


def tree_rss_mb(pid: int) -> int:
    """Resident memory of a process and all its descendants."""
    try:
        import psutil
        root = psutil.Process(pid)
        procs = [root] + root.children(recursive=True)
    except Exception:      # noqa: BLE001 - process already gone / psutil missing
        return 0
    total = 0
    for p in procs:
        try:
            total += p.memory_info().rss
        except Exception:      # noqa: BLE001
            continue
    return total >> 20


def scope_budget_mb(s: dict | None = None) -> int:
    s = s or settings()
    if s.get("scope_max_mb"):
        return int(s["scope_max_mb"])
    total, avail = mem_total_mb(), mem_available_mb()
    return max(2048, min(int(total * float(s["scope_max_fraction"])), avail - int(s["reserve_mb"])))


def plan_workers(requested: int | None = None, s: dict | None = None) -> int:
    """Worker count bounded by CPUs *and* by memory: (usable memory) / (expected memory per job)."""
    s = s or settings()
    cpu = max(1, (os.cpu_count() or 2) - 2)
    usable = mem_available_mb() - int(s["reserve_mb"])
    lim = cgroup_limit_mb()
    if lim is not None:
        usable = min(usable, lim) if usable > 0 else lim
    by_mem = max(1, usable // int(s["job_expect_mb"])) if usable > 0 else 1
    return max(1, min(requested or int(s["max_workers"]), cpu, by_mem))


# ----------------------------------------------------------------------------- layer 3: external tools
def child_limits() -> None:
    """``preexec_fn`` for external tools: no (piped) core dumps."""
    import resource
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (1, 1))
    except (ValueError, OSError):
        pass


# ----------------------------------------------------------------------------- layer 1: memory scope
def scope_status() -> dict:
    """Is the memory scope usable here? (executed probe, used by ``formbench doctor``)."""
    s = settings()
    if os.environ.get(ENV_IN_SCOPE):
        return {"status": "ok", "active": True, "limit_mb": cgroup_limit_mb()}
    exe = shutil.which("systemd-run")
    if not exe:
        return {"status": "blocked_by_environment", "active": False, "reason": "systemd-run not found; only the per-job watchdog protects the host"}
    try:
        p = subprocess.run([exe, "--user", "--scope", "-q", "-p", "MemoryMax=256M", "-p", "MemorySwapMax=0", sys.executable, "-c",
                            "from formbench.resources import cgroup_limit_mb as f; print(f())"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"status": "blocked_by_environment", "active": False, "reason": f"systemd-run failed: {e}"}
    if p.returncode != 0 or p.stdout.strip() != "256":
        return {"status": "blocked_by_environment", "active": False,
                "reason": f"memory controller not delegated to the user session (rc={p.returncode}, limit={p.stdout.strip()!r}); only the per-job watchdog protects the host"}
    return {"status": "ok", "active": False, "would_limit_mb": scope_budget_mb(s), "guard": s["guard"]}


def ensure_memory_scope(argv: list[str]) -> None:
    """Re-exec ``formbench <argv>`` inside a memory-capped systemd scope (no-op when already inside, disabled or unavailable)."""
    s = settings()
    if os.environ.get(ENV_IN_SCOPE):
        log_event("cli_start", argv=argv, scope="active", limit_mb=cgroup_limit_mb())
        return
    if str(s["guard"]).lower() == "off" or not argv or argv[0] in LIGHT_COMMANDS:
        return
    st = scope_status()
    if st["status"] != "ok":
        guard = str(s["guard"]).lower()
        heavy = argv[0] in HEAVY_COMMANDS
        log_event("scope_unavailable", argv=argv, reason=st.get("reason"), guard=guard, heavy=heavy)
        if guard == "required" or heavy:
            sys.stderr.write(f'{{"status": "blocked_by_environment", "error": "memory scope unavailable for a heavy command: {st.get("reason")}", '
                             f'"hint": "check `formbench doctor`; FORMBENCH_MEM_GUARD=off runs it unguarded at your own risk"}}\n')
            sys.exit(4)
        limit = scope_budget_mb(s)
        sys.stderr.write(f"formbench: memory scope unavailable ({st.get('reason')}); in-process watchdog at {limit} MB + per-job watchdog\n")
        start_self_watchdog(limit, float(s["poll_s"]) * 2)
        return
    mb = scope_budget_mb(s)
    env = dict(os.environ, **{ENV_IN_SCOPE: str(mb)})
    sys.stderr.write(f"formbench: running inside a memory scope (MemoryMax={mb}M, no swap)\n")
    sys.stderr.flush()
    os.execvpe("systemd-run", ["systemd-run", "--user", "--scope", "-q", "--collect", "-p", f"MemoryMax={mb}M", "-p", "MemorySwapMax=0",
                               sys.executable, "-m", "formbench.cli", *argv], env)


# ----------------------------------------------------------------------------- layer 2: isolated jobs
@dataclass
class JobOutcome:
    index: int
    status: str                # ok | error | memory_limit | crashed
    result: object = None
    error: str = ""
    peak_rss_mb: int = 0
    wall_s: float = 0.0
    attempts: int = 1


def _job_entry(fn, arg, out_path: str) -> None:
    try:
        os.setpgid(0, 0)                               # own process group: the job and its subprocesses are killed together
    except OSError:
        pass
    try:                                               # die with the parent (a killed CLI must not leave workers behind)
        import ctypes
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGKILL)
    except Exception:      # noqa: BLE001 - not Linux
        pass
    child_limits()
    try:
        payload = ("ok", fn(arg))
    except BaseException as e:      # noqa: BLE001 - reported to the parent, which decides
        payload = ("error", f"{type(e).__name__}: {e}"[:400] + "\n" + traceback.format_exc()[-1500:])
    tmp = out_path + ".tmp"
    with open(tmp, "wb") as fh:
        pickle.dump(payload, fh)
    os.replace(tmp, out_path)


def _kill_group(pid: int) -> None:
    for target in (lambda: os.killpg(pid, signal.SIGKILL), lambda: os.kill(pid, signal.SIGKILL)):
        try:
            target()
        except (ProcessLookupError, PermissionError, OSError):
            pass


def run_jobs(fn, jobs: list, workers: int | None = None, job_mem_mb: int | None = None, log=None, label: str = "job"):
    """Run ``fn(job)`` for every job, each in its own spawned process; yield a ``JobOutcome`` per job as it finishes.

    * ``memory_limit``: the job (process tree) exceeded ``job_mem_mb`` and was killed. Deterministic per job.
    * host pressure (MemAvailable below the reserve): the youngest job is killed and re-queued, concurrency drops by one.
    * abnormal exit not caused by the watchdog (e.g. the scope's OOM killer): retried once alone, then ``crashed``.
    """
    s = settings()
    cap = int(job_mem_mb or s["job_mem_mb"])
    reserve = int(s["reserve_mb"])
    poll = float(s["poll_s"])
    limit = max(1, workers or plan_workers(None, s))
    log = log or (lambda _m: None)
    ctx = mp.get_context("spawn")                      # never fork a multi-threaded parent
    tmp = tempfile.mkdtemp(prefix="fb_jobs_")
    pending = collections.deque(range(len(jobs)))
    solo: collections.deque = collections.deque()      # jobs that died abnormally once: retried with nothing else running
    attempts = [0] * len(jobs)
    running: dict[int, dict] = {}

    def launch(i: int, alone: bool = False) -> None:
        attempts[i] += 1
        out = os.path.join(tmp, f"{i}.{attempts[i]}.pkl")
        p = ctx.Process(target=_job_entry, args=(fn, jobs[i], out), daemon=False)
        p.start()
        running[i] = {"p": p, "out": out, "t0": time.monotonic(), "peak": 0, "flag": None, "solo": alone}

    try:
        while pending or solo or running:
            if solo and not running:
                launch(solo.popleft(), alone=True)
            while pending and not solo and len(running) < limit and not any(r["solo"] for r in running.values()):
                avail = mem_available_mb()
                if running and avail and avail < reserve + cap:
                    break                              # not enough headroom for one more job right now
                launch(pending.popleft())
            mpc.wait([r["p"].sentinel for r in running.values()], timeout=poll)
            for i, r in list(running.items()):
                p = r["p"]
                if p.is_alive():
                    rss = tree_rss_mb(p.pid)
                    r["peak"] = max(r["peak"], rss)
                    if rss > cap and r["flag"] is None:
                        r["flag"] = "memory_limit"
                        _kill_group(p.pid)
                    continue
                p.join()
                del running[i]
                wall = round(time.monotonic() - r["t0"], 1)
                _kill_group(p.pid)                     # stragglers of the job's process group
                if r["flag"] == "memory_limit":
                    log_event("job_memory_limit", label=label, job=i, peak_mb=r["peak"], cap_mb=cap)
                    yield JobOutcome(i, "memory_limit", None, f"resident memory {r['peak']} MB exceeded the per-job cap of {cap} MB", r["peak"], wall, attempts[i])
                elif r["flag"] == "requeue":
                    log_event("job_requeued_host_pressure", label=label, job=i, peak_mb=r["peak"])
                    attempts[i] -= 1
                    pending.appendleft(i)
                elif p.exitcode == 0 and os.path.exists(r["out"]):
                    with open(r["out"], "rb") as fh:
                        kind, val = pickle.load(fh)
                    os.unlink(r["out"])
                    yield JobOutcome(i, kind, val if kind == "ok" else None, "" if kind == "ok" else val, r["peak"], wall, attempts[i])
                elif attempts[i] < 2:
                    log(f"{label} {i} exited abnormally (exit code {p.exitcode}); retrying it alone")
                    solo.append(i)
                else:
                    yield JobOutcome(i, "crashed", None, f"process exited with code {p.exitcode} twice (killed by the memory scope or a native crash)",
                                     r["peak"], wall, attempts[i])
            avail = mem_available_mb()
            live = [(r["t0"], i) for i, r in running.items() if r["flag"] is None]
            if avail and avail < reserve // 2 and len(live) > 1:
                _, i = max(live)                       # youngest job loses the least work
                running[i]["flag"] = "requeue"
                _kill_group(running[i]["p"].pid)
                limit = max(1, len(live) - 1)
                log(f"host memory low ({avail} MB available): re-queued {label} {i}, concurrency -> {limit}")
    finally:
        for r in running.values():
            _kill_group(r["p"].pid)
        shutil.rmtree(tmp, ignore_errors=True)


def in_order(outcomes):
    """Re-order a ``run_jobs`` stream by job index (side effects that must not depend on scheduling)."""
    buf: dict[int, JobOutcome] = {}
    nxt = 0
    for o in outcomes:
        buf[o.index] = o
        while nxt in buf:
            yield buf.pop(nxt)
            nxt += 1
    for i in sorted(buf):
        yield buf[i]
