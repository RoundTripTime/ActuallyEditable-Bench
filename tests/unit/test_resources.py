"""Host resource guards (regressions for the host OOM of 2026-10-14): isolated jobs with a memory watchdog, external
tools without core dumps or stragglers, a search open list that holds no document snapshots, crash-tolerant progress."""
import os
import sys
import time

from conftest import ASSETS, contracts_for
from formbench import fixtures, resources
from formbench.planning import search
from formbench.util import run_cmd


def job_square(x):
    if x == "boom":
        raise ValueError("bad input")
    return x * x


def job_hog(x):
    if x == "hog":
        blocks = []
        while True:                                   # grows until the watchdog kills the job
            blocks.append(bytearray(32 << 20))
            time.sleep(0.02)
    return x


def job_exit(marker):
    if marker == "always" or not os.path.exists(marker):
        if marker != "always":
            open(marker, "w").close()
        os._exit(3)                                   # abnormal exit without a result
    return "recovered"


def test_run_jobs_returns_every_result_and_reports_python_errors():
    out = sorted(resources.run_jobs(job_square, [1, 2, "boom", 4], workers=2), key=lambda o: o.index)
    assert [o.status for o in out] == ["ok", "ok", "error", "ok"]
    assert [o.result for o in out] == [1, 4, None, 16] and "ValueError: bad input" in out[2].error
    assert [o.index for o in resources.in_order(iter(out[::-1]))] == [0, 1, 2, 3]


def test_job_over_the_memory_cap_is_killed_alone():
    out = {o.index: o for o in resources.run_jobs(job_hog, ["a", "hog", "b"], workers=3, job_mem_mb=200)}
    assert out[1].status == "memory_limit" and out[1].peak_rss_mb > 200 and "per-job cap of 200 MB" in out[1].error
    assert out[1].peak_rss_mb < 1500                  # stopped long before it could hurt the host
    assert (out[0].status, out[0].result, out[2].status, out[2].result) == ("ok", "a", "ok", "b")


def test_abnormal_exit_is_retried_once_alone_then_reported(tmp_path):
    out = {o.index: o for o in resources.run_jobs(job_exit, [str(tmp_path / "once"), "always"], workers=2)}
    assert (out[0].status, out[0].result, out[0].attempts) == ("ok", "recovered", 2)
    assert out[1].status == "crashed" and out[1].attempts == 2 and "code 3" in out[1].error


def test_external_tools_run_without_core_dumps_and_leave_no_stragglers(tmp_path):
    rc, out, _ = run_cmd([sys.executable, "-c", "import resource; print(resource.getrlimit(resource.RLIMIT_CORE))"])
    assert rc == 0 and out.strip() == "(1, 1)"        # the kernel skips piped core dumps (wsl-capture-crash) only for a limit of 1
    pidfile = tmp_path / "pid"
    rc, _, err = run_cmd(["sh", "-c", f"sleep 60 & echo $! > {pidfile}; wait"], timeout=1)
    assert (rc, err) == (-9, "timeout")
    pid = int(pidfile.read_text())
    for _ in range(50):
        if not os.path.exists(f"/proc/{pid}") or open(f"/proc/{pid}/stat").read().split()[2] == "Z":
            break
        time.sleep(0.1)
    else:
        raise AssertionError("grandchild of a timed-out tool is still running")


def test_worker_count_is_bounded_by_memory(monkeypatch):
    s = dict(resources.DEFAULTS, job_expect_mb=1024, reserve_mb=4096, max_workers=8)
    monkeypatch.setattr(resources, "cgroup_limit_mb", lambda: None)
    monkeypatch.setattr(resources, "mem_available_mb", lambda: 4096 + 3 * 1024)
    assert resources.plan_workers(14, s) <= 3
    monkeypatch.setattr(resources, "mem_available_mb", lambda: 1000)
    assert resources.plan_workers(14, s) == 1
    monkeypatch.setattr(resources, "mem_available_mb", lambda: 64000)
    monkeypatch.setattr(resources, "cgroup_limit_mb", lambda: 2048)
    assert resources.plan_workers(14, s) <= 2


def test_search_open_list_stores_plans_not_document_states(monkeypatch):
    """Each open-list entry used to carry a document snapshot and a layout view (~6 MB): gigabytes on wide searches."""
    pushed = []

    class Heap:
        @staticmethod
        def heappush(h, item):
            pushed.append(item)
            import heapq
            heapq.heappush(h, item)

        @staticmethod
        def heappop(h):
            import heapq
            return heapq.heappop(h)
    monkeypatch.setattr(search, "heapq", Heap)
    data = fixtures.build("long_text_form")
    for c in contracts_for(data).values():
        res = search.Planner(c, assets=ASSETS).solve(data)
        assert res.status in ("exact", "bounded", "infeasible")
    assert pushed
    for item in pushed:
        assert len(item) == 8 and isinstance(item[6], list) and isinstance(item[7], frozenset)
        assert all(isinstance(x, (int, str)) for x in item[:6])


def _no_scope(monkeypatch, sandbox):
    monkeypatch.delenv(resources.ENV_IN_SCOPE, raising=False)
    monkeypatch.setenv("FORMBENCH_MEM_GUARD", "auto")
    monkeypatch.setattr(resources, "scope_status", lambda: {"status": "blocked_by_environment", "active": False, "reason": "test: no delegation"})
    started = []
    monkeypatch.setattr(resources, "start_self_watchdog", lambda limit, poll_s=1.0, log=None: started.append(limit))
    monkeypatch.setattr(resources.os, "execvpe", lambda *a: (_ for _ in ()).throw(AssertionError("must not re-exec without a scope")))
    return started


def test_heavy_command_does_not_start_without_a_memory_scope(monkeypatch, sandbox):
    # 2026-10-15: a WSL update changed cgroup delegation; heavy stages must not run unguarded silently
    _no_scope(monkeypatch, sandbox)
    import pytest
    with pytest.raises(SystemExit) as e:
        resources.ensure_memory_scope(["benchmark", "--dataset", "eval"])
    assert e.value.code == 4
    events = [l for l in resources.path(*resources.EVENT_LOG).read_text().splitlines() if '"scope_unavailable"' in l]
    assert events and '"heavy": true' in events[-1]


def test_light_bookkeeping_falls_back_to_the_in_process_watchdog(monkeypatch, sandbox):
    started = _no_scope(monkeypatch, sandbox)
    resources.ensure_memory_scope(["corpus", "pair"])
    assert started and started[0] >= 2048


def test_self_watchdog_ends_a_runaway_process_tree(tmp_path):
    code = ("import time, formbench.resources as r\n"
            "r.start_self_watchdog(300, 0.1)\n"
            "b=[]\n"
            "for _ in range(200):\n    b.append(bytearray(16<<20)); time.sleep(0.02)\n"
            "print('not stopped')\n")
    import subprocess
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                       env=dict(os.environ, FORMBENCH_ROOT=str(tmp_path)))
    assert p.returncode == 137 and "not stopped" not in p.stdout
