"""Experiment current supplement (user request 2026-10-19): what if GPT-6-Luna's 17 format failures had been repaired?

In the main run (scripts/reconstruct_gpt.py, two requests per chunk) 17 tasks failed because the answer was not
well-formed XML (mostly a closing tag that does not match its element, e.g. <table:table-cell> ... </table:cell>), so
they were scored as editing from an empty document. Here each failed chunk gets a repair loop:
  1. feedback: the same pages, prompt and ODF specification, plus the failed answer as ./previous_answer.xml and the XML
     parser's error message; the model must return the complete corrected document (up to 3 rounds);
  2. retry: if still not well-formed, one new request (same as the main run), followed by up to 2 feedback rounds.
Chunks that were never requested in the main run (documents over 8 pages failed at chunk 1) start with a new request.
Same model, reasoning effort, prompt, specification files and packing as the main run.
Output label png_150__gpt6luna_odt_fix in data/experiment_documents/<task>/recon/ (only for the failed tasks); the main results are not
changed. `eval` scores visual similarity and edit time (KLM, current example) for these files; `report` substitutes them into
the GPT-6-Luna rows of main and writes reports/experiments/gpt-repair/summary.json.
Usage: FORMBENCH_OBS=experiment_documents python scripts/gpt_repair.py run [--workers 6] | eval | report
"""
from __future__ import annotations

import argparse
import csv
import glob
import importlib.util
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

os.environ.setdefault("FORMBENCH_OBS", "experiment_documents")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
from formbench.util import obs_name, path, sha256_bytes, utcnow, write_json  # noqa: E402


def _mod(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


G = _mod("gpt_odt", "reconstruct_gpt.py")
BASE = G.LABEL                                   # png_150__gpt6luna_odt
LABEL = BASE + "_fix"
FEEDBACK_ROUNDS, RETRY_FEEDBACK_ROUNDS = 3, 2
REPORT = Path(path("reports", "experiments", "gpt-repair"))
REPAIR = ("\n\nREPAIR REQUEST: your previous answer for these pages is in ./previous_answer.xml. It is not a well-formed "
          "single-file OpenDocument document; the XML parser reports:\n{error}\nRead that file, correct every error of this "
          "kind in the whole document, keep everything else unchanged, and answer with the complete corrected document only "
          "(from <?xml version=\"1.0\" encoding=\"UTF-8\"?> to </office:document>).\n")


def call(images: list[Path], prompt: str, previous: str | None) -> dict:
    """G.call with an optional previous answer placed in the working directory (read-only sandbox can read it)."""
    with tempfile.TemporaryDirectory(prefix="fb_codex_fix_") as td:
        (Path(td) / "odf_spec").mkdir()
        for f in G.SPEC_FILES:
            (Path(td) / "odf_spec" / f).write_bytes((G.SPEC / f).read_bytes())
        if previous is not None:
            (Path(td) / "previous_answer.xml").write_text(previous, encoding="utf-8")
        out = Path(td) / "last.xml"
        cmd = ["codex", "exec", "-m", G.MODEL, "-c", f'model_reasoning_effort="{G.EFFORT}"', "-s", "read-only", "--ephemeral",
               "--skip-git-repo-check", "-o", str(out)]
        for im in images:
            cmd += ["-i", str(im)]
        cmd += ["-"]
        t0 = time.monotonic()
        try:
            p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=td, timeout=2400)
        except subprocess.TimeoutExpired:
            return {"error": "codex call exceeded 2400 s", "seconds": round(time.monotonic() - t0, 1)}
        log = (p.stdout or "") + (p.stderr or "")
        m = re.search(r"tokens used\s*\n\s*([\d,]+)", log)
        tokens = int(m.group(1).replace(",", "")) if m else None
        ans = out.read_text(encoding="utf-8", errors="replace") if out.exists() else ""
        a, b = ans.find("<?xml"), ans.rfind("</office:document>")
        if a >= 0 and b > a:
            return {"xml": ans[a:b + len("</office:document>")], "tokens": tokens, "seconds": round(time.monotonic() - t0, 1)}
        errs = [ln for ln in log.splitlines() if ln.startswith("ERROR") or "error" in ln.lower()[:20]]
        msg = " | ".join(errs)[-600:] or log[-600:]
        if G.LIMIT_RE.search(msg):
            return {"limit": msg}
        return {"error": msg or f"rc={p.returncode}, no ODF answer", "tokens": tokens, "seconds": round(time.monotonic() - t0, 1)}


def request(images, previous, error, log, rd) -> dict:
    prompt = G.PROMPT.read_text(encoding="utf-8") + (REPAIR.format(error=error) if previous is not None else "")
    while True:
        res = call(images, prompt, previous)
        if "limit" not in res:
            return res
        log(json.dumps({"event": "usage_limit", "task": rd.parent.name[:24], "message": res["limit"][:200], "at": utcnow()}))
        time.sleep(1800)


def check(xml: str) -> str | None:
    try:
        G.pack_fodt(xml.encode("utf-8"))
        return None
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"[:300]


def repair_chunk(images, previous, error, rd, tag, log) -> tuple[str | None, list]:
    """Returns (well-formed xml or None, attempts)."""
    att = []
    plan = ["feedback"] * FEEDBACK_ROUNDS + ["retry"] + ["feedback"] * RETRY_FEEDBACK_ROUNDS
    if previous is None:                                 # never requested in the main run: start like the main run
        plan = ["retry"] + ["feedback"] * FEEDBACK_ROUNDS
    for kind in plan:
        if kind == "feedback" and previous is None:
            continue
        res = request(images, previous if kind == "feedback" else None, error, log, rd)
        rec = {"kind": kind, **{k: v for k, v in res.items() if k != "xml"}}
        if "xml" in res:
            err = check(res["xml"])
            rec["packed"] = err is None
            (rd / f"{LABEL}{tag}.try{len(att) + 1}.fodt").write_text(res["xml"], encoding="utf-8")
            if err is None:
                att.append(rec)
                return res["xml"], att
            rec["pack_error"] = err
            previous, error = res["xml"], err
        att.append(rec)
    return None, att


def job(c: dict, log) -> None:
    d = G.task_dir(c)
    rd = d / "recon"
    if (rd / f"{LABEL}.json").exists():
        return
    pg = G.pages(d)
    chunks = [pg[i:i + G.MAX_PAGES] for i in range(0, len(pg), G.MAX_PAGES)]
    base = json.load(open(rd / f"{BASE}.json"))
    parts, attempts = [], []
    for k, ch in enumerate(chunks):
        tag = f".chunk{k + 1}" if len(chunks) > 1 else ""
        prev_f = sorted(rd.glob(f"{BASE}{tag}.try*.fodt"))
        previous = prev_f[-1].read_text(encoding="utf-8") if prev_f else None
        error = check(previous) if previous is not None else None
        if previous is not None and error is None:       # this chunk already succeeded in the main run (never saved for
            parts.append(previous)                       # multi-chunk failures, kept for completeness)
            continue
        xml, att = repair_chunk(ch, previous, error, rd, tag, log)
        attempts += [a | {"chunk": k + 1} for a in att]
        if xml is None:
            break
        parts.append(xml)
    rec = {"task_id": c["task_id"], "input": "png_150", "baseline": "gpt6luna_odt_fix", "started_at": utcnow(),
           "main_run_reason": base.get("reason"), "attempts": attempts, "requests": len(attempts),
           "info": {"tool": f"{G.MODEL} via {G.codex_version()} (reasoning {G.EFFORT})", "prompt_sha256": sha256_bytes(G.PROMPT.read_bytes()),
                    "repair_prompt": REPAIR, "rounds": {"feedback": FEEDBACK_ROUNDS, "retry_feedback": RETRY_FEEDBACK_ROUNDS}}}
    if len(parts) == len(chunks):
        x = (parts[0] if len(parts) == 1 else G.merge_fodt(parts)).encode("utf-8")
        odt = G.pack_fodt(x)
        (rd / f"{LABEL}.fodt").write_bytes(x)
        (rd / f"{LABEL}.odt").write_bytes(odt)
        rec.update(status="ok", sha256=sha256_bytes(odt), **G.rng_check(x),
                   fixed_by=sorted({a["kind"] for a in attempts if a.get("packed")}))
    else:
        rec.update(status="failed", reason=str(attempts[-1].get("pack_error") or attempts[-1].get("error", ""))[:300] if attempts else "")
    write_json(rd / f"{LABEL}.json", rec)
    log(json.dumps({"task_id": c["task_id"][:24], "status": rec["status"], "pages": len(pg), "requests": len(attempts),
                    "fixed_by": rec.get("fixed_by")}))


def failed_tasks() -> list[dict]:
    out = []
    for c in G.all_tasks():
        f = G.task_dir(c) / "recon" / f"{BASE}.json"
        if f.exists() and json.load(open(f))["status"] != "ok":
            out.append(c)
    return out


def evaluate() -> None:
    os.environ["FORMBENCH_COST_UNITS"] = "klm"
    sourcev = _mod("sourcev", "visual_helpers.py")
    et = _mod("et", "edit_breakdown.py")
    from formbench.completion.sessions import open_session
    from formbench.editing.cost import document_cost
    files = [str(G.task_dir(c) / "recon" / f"{LABEL}.odt") for c in failed_tasks() if (G.task_dir(c) / "recon" / f"{LABEL}.odt").exists()]
    sourcev.batch(files)
    out = Path(path("data", "runs", "gpt-repair-cost", LABEL))
    out.mkdir(parents=True, exist_ok=True)
    for f in files:
        d = Path(f).parent.parent
        c = json.load(open(path("data", "observed_contracts", d.name + ".json")))
        ds, es = open_session(Path(f).read_bytes(), "odt"), open_session((d / "example.odt").read_bytes(), "odt")
        r = document_cost(ds, es, lang=c.get("language"))
        parts, by_op, cost = et.doc_parts(open_session(Path(f).read_bytes(), "odt"), es, c.get("language"))
        write_json(out / f"{d.name}.json", {"task_id": c["task_id"], "label": LABEL, "cost": r["cost"], "cost_edit_tasks": cost,
                                           "by_op": by_op, "computed_at": utcnow(), "unit": "seconds"})
    print(f"# evaluated {len(files)}")


def report() -> None:
    from scipy.stats import spearmanr
    rows = list(csv.DictReader(open(path("reports", "experiments", "main", "rows.csv"), encoding="utf-8")))
    orig = {r["task_id"]: float(r["cost_T"]) for r in rows}
    fix = {}
    for f in glob.glob(str(path("data", "runs", "gpt-repair-cost", LABEL, "*.json"))):
        j = json.load(open(f))
        tid = j["task_id"].replace("~", "__")
        vis = json.load(open(path("data", obs_name(), tid, "recon", f"{LABEL}.visual.json")))
        fix[j["task_id"]] = {"ratio": j["cost"] / max(1, orig[j["task_id"]]), "cost": j["cost"],
                             "ssim": vis["ssim_blur"] * max(vis["pages_native"], vis["pages_recon"]) / vis["pages_native"],
                             "extra": vis["pages_recon"] > vis["pages_native"], "by_op": j["by_op"]}
    gpt = [r for r in rows if r["label"] == BASE]
    failed = {r["task_id"] for r in gpt if r["from_empty"] == "True"}
    recs = [json.load(open(G.task_dir(c) / "recon" / f"{LABEL}.json")) for c in failed_tasks()]

    def stats(sub: list[dict], use_fix: bool) -> dict:
        rat, ss, ex = [], [], []
        for r in sub:
            if use_fix and r["task_id"] in fix:
                x = fix[r["task_id"]]
                rat.append(x["ratio"]); ss.append(x["ssim"]); ex.append(x["extra"])
            else:
                rat.append(float(r["ratio"]))
                if r["ssim_blur_native_pages"] not in ("", "None"):
                    ss.append(float(r["ssim_blur_native_pages"])); ex.append(r["extra_pages"] == "True")
        return {"tasks": len(sub), "ratio_median": round(statistics.median(rat), 3),
                "ssim_median": round(statistics.median(ss), 4) if ss else None,
                "extra_pages_share": round(sum(ex) / len(ex), 4) if ex else None, "with_visual": len(ss)}
    four = [r for r in rows if r["label"] in ("vector_pdf__word_pdf", "vector_pdf__hwp_pdf", BASE, "vector_pdf__docling_odt")]

    def corr(use_fix: bool) -> dict:
        pts = [(float(r["ssim_blur_native_pages"]), float(r["ratio"])) for r in four if r["ssim_blur_native_pages"] not in ("", "None")]
        if use_fix:
            pts += [(x["ssim"], x["ratio"]) for x in fix.values()]
        hi = [y for x, y in pts if x >= 0.9]
        return {"n": len(pts), "spearman": round(float(spearmanr([p[0] for p in pts], [p[1] for p in pts]).statistic), 3),
                "ssim_ge_0_9": {"n": len(hi), "ratio_median": round(statistics.median(hi), 3)}}
    sub_failed = [r for r in gpt if r["task_id"] in failed]
    summary = {"created_at": utcnow(), "label": LABEL, "failed_in_main_run": len(failed),
               "repaired": sum(1 for r in recs if r["status"] == "ok"),
               "fixed_by": {k: sum(1 for r in recs if r.get("fixed_by") == [k]) for k in ("feedback", "retry")},
               "requests": [r["requests"] for r in recs],
               "gpt_all_tasks": {"main": stats(gpt, False), "with_repair": stats(gpt, True)},
               "failed_tasks_only": {"main_from_empty": stats(sub_failed, False), "repaired": stats(sub_failed, True)},
               "four_methods_correlation": {"main": corr(False), "with_repair": corr(True)},
               "per_task": {t: {k: (round(v, 4) if isinstance(v, float) else v) for k, v in x.items() if k != "by_op"} for t, x in fix.items()}}
    REPORT.mkdir(parents=True, exist_ok=True)
    write_json(REPORT / "summary.json", summary)
    print(json.dumps({k: v for k, v in summary.items() if k != "per_task"}, ensure_ascii=False, indent=1))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "eval", "report"])
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    if a.cmd == "run":
        ts = failed_tasks()
        print(f"# {len(ts)} failed tasks, model {G.MODEL}, {G.codex_version()}", flush=True)
        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            for f in [ex.submit(job, c, lambda s: print(s, flush=True)) for c in ts]:
                f.result()
    elif a.cmd == "eval":
        evaluate()
    else:
        report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
