"""Experiment current supplement (user request 2026-10-20): GPT-6-Luna given the digital PDF as well as the page images.

Main run (scripts/reconstruct_gpt.py, label png_150__gpt6luna_odt): the page images (png_150) + the official ODF 1.4
specification. This condition adds the digital PDF with its text layer that the page images were rendered from
(inputs/vector_pdf.pdf = native.pdf, the LibreOffice rendering of the preprocessed template; the same PDF the Word, Hangul
and Docling reconstructions read) as ./input/form.pdf, unchanged, in the Codex working directory. Codex attaches images
only, so the model reads the PDF with commands; its streams are Flate-compressed, so the prompt allows read-only commands
including python3 with its standard library only (no PDF library, converter or office application; user decision).
Everything else is the main run's: model, reasoning effort, read-only sandbox, rules and output format (prompt
configs/prompts/vlm_form_odt_pdf_v1.txt = configs/prompts/vlm_form_odt_v1.txt + the PDF paragraph + the reading rule),
chunks of 8 pages, two requests per chunk, packing (pack_fodt) and merging, a failed task scored as writing the example
from an empty document. One addition only for the 9 documents over 8 pages: each chunk's prompt says which pages its
images are (pages a to b of n) and to reconstruct only those, because the PDF always covers the whole document.
Codex service outages (no connection) are environment faults, not answers: the request is repeated after a pause (ENV_RE).

Label png_150__gpt6luna_odt_pdf in data/experiment_documents/<task>/recon/. Scoring: the current definitions (visual similarity to the
LibreOffice rendering of template.odt, edit time to example.odt; KLM seconds and action counts) in run dirs
data/runs/gpt-pdf-cost and data/runs/gpt-pdf-actions (the originals' costs copied from main / actions).
`report` compares the condition with the main run per task -> reports/experiments/gpt-pdf/.
Usage: FORMBENCH_OBS=experiment_documents python scripts/gpt_pdf.py run [--workers 6] [--limit N] [--tasks ID,...]
       ... visual | cost --units klm|actions | report
"""
from __future__ import annotations

import argparse
import csv
import glob
import importlib.util
import json
import math
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
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
BASE = G.LABEL                                       # png_150__gpt6luna_odt
LABEL = BASE + "_pdf"
PROMPT = Path(path("configs", "prompts", "vlm_form_odt_pdf_v1.txt"))
RUNS = {"klm": ("main", "gpt-pdf-cost"), "actions": ("actions", "gpt-pdf-actions")}
REPORT = Path(path("reports", "experiments", "gpt-pdf"))
CMD_RE = re.compile(r"^exec\n(.*)$", re.M)
OUTAGE = Path(path("data", "logs", "current_gpt_pdf_env_failures", "tasks.txt"))
# Codex could not reach the service (2026-10-20 17:56-18:48 KST: "workspace routing discovery failed" after 5 reconnects);
# like a usage limit this is not an answer, so the request is repeated after a pause instead of counting as one of the two
ENV_RE = re.compile(r"routing discovery failed|Reconnecting\.\.\. 5/5|stream disconnected", re.I)


def pdf_path(c: dict) -> Path:
    return G.task_dir(c) / "inputs" / "vector_pdf.pdf"


def prompt_text(first: int, last: int, total: int) -> str:
    note = "" if (first == 1 and last == total) else (f" These images are pages {first} to {last} of the {total} pages of the document;"
                                                      " reconstruct only these pages (the part of the PDF that they show).")
    return PROMPT.read_text(encoding="utf-8").replace("{pages_note}", note)


def call(images: list[Path], prompt: str, pdf: bytes) -> dict:
    """G.call with the PDF placed next to the ODF specification (./input/form.pdf)."""
    with tempfile.TemporaryDirectory(prefix="fb_codex_pdf_") as td:
        (Path(td) / "odf_spec").mkdir()
        for f in G.SPEC_FILES:
            (Path(td) / "odf_spec" / f).write_bytes((G.SPEC / f).read_bytes())
        (Path(td) / "input").mkdir()
        (Path(td) / "input" / "form.pdf").write_bytes(pdf)
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
        commands = len(re.findall(r"^exec\b|^\[[^\]]*\] exec ", log, re.M))
        cmds = CMD_RE.findall(log)
        reads = {"pdf_commands": sum(1 for x in cmds if "form.pdf" in x or re.search(r"\binput\b", x)),
                 "python_commands": sum(1 for x in cmds if "python" in x),
                 "spec_commands": sum(1 for x in cmds if "odf_spec" in x)}
        ans = out.read_text(encoding="utf-8", errors="replace") if out.exists() else ""
        a, b = ans.find("<?xml"), ans.rfind("</office:document>")
        if a >= 0 and b > a:
            return {"xml": ans[a:b + len("</office:document>")], "tokens": tokens, "commands": commands, **reads,
                    "seconds": round(time.monotonic() - t0, 1), "log_tail": log[-40000:]}
        errs = [ln for ln in log.splitlines() if ln.startswith("ERROR") or "error" in ln.lower()[:20]]
        msg = " | ".join(errs)[-600:] or log[-600:]
        if G.LIMIT_RE.search(msg):
            return {"limit": msg}
        if ENV_RE.search(msg):
            return {"env": msg}
        return {"error": msg or f"rc={p.returncode}, no ODF answer", "tokens": tokens, "seconds": round(time.monotonic() - t0, 1)}


def answer(images, prompt, pdf: bytes, rd: Path, tag: str, log) -> tuple[dict, list]:
    """One chunk: up to 2 requests until the answer is well-formed ODF that packs (as the main run); usage limits are
    not results: wait and retry the same request."""
    attempts, res = [], {}
    for _ in range(2):
        while True:
            res = call(images, prompt, pdf)
            if "env" in res:                             # Codex service unreachable: an environment fault, not an answer
                log(json.dumps({"event": "env_error", "task": rd.parent.name[:24], "message": res["env"][-200:], "at": utcnow()}))
                time.sleep(300)
                continue
            if "limit" not in res:
                break
            log(json.dumps({"event": "usage_limit", "task": rd.parent.name[:24], "message": res["limit"][:200], "at": utcnow()}))
            time.sleep(1800)
        rec_try = {k: v for k, v in res.items() if k not in ("xml", "log_tail")}
        if res.get("log_tail"):
            (rd / f"{LABEL}{tag}.try{len(attempts) + 1}.codex.log").write_text(res["log_tail"], encoding="utf-8")
        if "xml" in res:
            try:
                G.pack_fodt(res["xml"].encode("utf-8"))
                attempts.append(rec_try | {"packed": True})
                return res, attempts
            except Exception as e:  # noqa: BLE001
                attempts.append(rec_try | {"packed": False, "pack_error": f"{type(e).__name__}: {e}"[:300]})
                (rd / f"{LABEL}{tag}.try{len(attempts)}.fodt").write_text(res["xml"], encoding="utf-8")
                res = {"error": attempts[-1]["pack_error"]}
        else:
            attempts.append(rec_try)
    return res, attempts


def job(c: dict, log=print) -> None:
    d = G.task_dir(c)
    rd = d / "recon"
    if (rd / f"{LABEL}.json").exists():
        return
    pg = G.pages(d)
    if not pg:
        return
    pdf = pdf_path(c).read_bytes()
    chunks = [pg[i:i + G.MAX_PAGES] for i in range(0, len(pg), G.MAX_PAGES)]
    parts, attempts, res, notes = [], [], {}, []
    for k, ch in enumerate(chunks):
        first = k * G.MAX_PAGES + 1
        prompt = prompt_text(first, first + len(ch) - 1, len(pg))
        notes.append(sha256_bytes(prompt.encode("utf-8")))
        res, att = answer(ch, prompt, pdf, rd, f".chunk{k + 1}" if len(chunks) > 1 else "", log)
        attempts += [a | {"chunk": k + 1} for a in att] if len(chunks) > 1 else att
        if "xml" not in res:
            break
        parts.append(res["xml"])
    rec = {"task_id": c["task_id"], "input": "png_150+vector_pdf", "baseline": "gpt6luna_odt_pdf", "family": "B2-vlm-api",
           "started_at": utcnow(), "attempts": attempts, "requests": len(parts),
           "info": {"tool": f"{G.MODEL} via {G.codex_version()} (reasoning {G.EFFORT})",
                    "prompt_template_sha256": sha256_bytes(PROMPT.read_bytes()), "prompt_sha256": notes,
                    "spec": {f: sha256_bytes((G.SPEC / f).read_bytes()) for f in G.SPEC_FILES},
                    "pdf": {"file": "inputs/vector_pdf.pdf", "sha256": sha256_bytes(pdf), "bytes": len(pdf)},
                    "via": "single-file ODF -> ODT package (pack_fodt)", "external_service": "OpenAI (Codex CLI, ChatGPT subscription)"}}
    if len(parts) == len(chunks):
        x = (parts[0] if len(parts) == 1 else G.merge_fodt(parts)).encode("utf-8")
        odt = G.pack_fodt(x)
        (rd / f"{LABEL}.fodt").write_bytes(x)
        (rd / f"{LABEL}.odt").write_bytes(odt)
        ok = [a for a in attempts if a.get("packed")]
        rec.update(status="ok", sha256=sha256_bytes(odt), elapsed_s=round(sum(a.get("seconds") or 0 for a in ok), 1),
                   tokens=sum(a.get("tokens") or 0 for a in ok), commands=sum(a.get("commands") or 0 for a in ok),
                   pdf_commands=sum(a.get("pdf_commands") or 0 for a in ok),
                   python_commands=sum(a.get("python_commands") or 0 for a in ok), **G.rng_check(x))
    else:
        rec.update(status="failed", reason=str(res.get("error", ""))[:300])
    write_json(rd / f"{LABEL}.json", rec)
    log(json.dumps({"task_id": c["task_id"][:24], "format": c["format"], "status": rec["status"], "pages": len(pg), "tokens": rec.get("tokens"),
                    "s": rec.get("elapsed_s"), "rng_valid": rec.get("rng_valid"), "commands": rec.get("commands"),
                    "pdf_commands": rec.get("pdf_commands"), "python_commands": rec.get("python_commands")}, ensure_ascii=False))


# ---------------------------------------------------------------- scoring (current definitions)
_MODS: dict = {}


def _cached(name: str, file: str):
    if name not in _MODS:
        _MODS[name] = _mod(name, file)
    return _MODS[name]


def _visual_batch(odts: list[str]) -> list:
    """Worker entry point (a function of a module loaded from a file path cannot be pickled for the pool)."""
    return _cached("editv", "evaluate_visual.py").batch(odts)


def _cost_job(args) -> list[dict]:
    vc = _cached("editc", "evaluate_cost.py")
    vc.LABELS = [LABEL]
    return vc.task_job(args)


def visual(workers: int) -> None:
    vis = _cached("editv", "evaluate_visual.py")
    todo = [f for f in sorted(glob.glob(str(path("data", obs_name(), "*", "recon", f"{LABEL}.odt")))) if vis.stale(Path(f))]
    print(f"# visual: {len(todo)} to score", flush=True)
    chunks = [todo[i:i + 25] for i in range(0, len(todo), 25)]
    k = 0
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for fut in as_completed([ex.submit(_visual_batch, ch) for ch in chunks]):
            k += len(fut.result())
            print(f"{k}/{len(todo)}", flush=True)


def cost(workers: int, unit: str) -> None:
    # explicit unit: importing reconstruct_gpt.py (G) defaults FORMBENCH_COST_UNITS to klm, so the environment alone is
    # not a reliable switch; the pool's workers are forked after this and read it when the cost table is first loaded
    if unit == "klm":
        os.environ["FORMBENCH_COST_UNITS"] = "klm"
    else:
        os.environ.pop("FORMBENCH_COST_UNITS", None)
    main_run, run = RUNS[unit]
    run_dir = Path(path("data", "runs", run))
    (run_dir / "original").mkdir(parents=True, exist_ok=True)
    for f in glob.glob(str(path("data", "runs", main_run, "original", "*.json"))):   # the same originals as the main report
        dst = run_dir / "original" / Path(f).name
        if not dst.exists():
            shutil.copy2(f, dst)
    cs = sorted(glob.glob(str(path("data", "observed_contracts", "doc-*.json"))))
    n = 0
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for fut in as_completed([ex.submit(_cost_job, (c, str(run_dir))) for c in cs]):
            n += 1
            for lg in fut.result():
                if lg["status"] != "ok":
                    print(json.dumps(lg), flush=True)
    print(f"# {unit}: {n} tasks -> {run_dir}", flush=True)


# ---------------------------------------------------------------- report
def _rows(unit: str) -> list[dict]:
    """Main-report rows (main / actions) + rows of this condition built with the same definitions (report.build)."""
    main_run, run = RUNS[unit]
    rows = []
    for r in csv.DictReader(open(path("reports", "experiments", main_run, "rows.csv"), encoding="utf-8")):
        x = dict(r)
        for k in ("cost_R", "cost_T", "ratio", "ssim_blur", "ssim_blur_native_pages"):
            x[k] = float(x[k]) if x[k] not in ("", "None") else None
        for k in ("from_empty", "extra_pages"):
            x[k] = {"True": True, "False": False}.get(x[k])
        rows.append(x)
    cost_T = {x["task_id"]: x["cost_T"] for x in rows}
    meta = {x["task_id"]: x for x in rows}
    for f in sorted(glob.glob(str(path("data", "runs", run, LABEL, "*.json")))):
        r = json.load(open(f))
        if r.get("status") != "ok" or r["task_id"] not in cost_T:
            continue
        tid = r["task_id"].replace("~", "__")
        vf = Path(path("data", obs_name(), tid, "recon", f"{LABEL}.visual.json"))
        v = json.load(open(vf)) if vf.exists() else {}
        ok = v.get("status") == "ok"
        m = meta[r["task_id"]]
        rows.append({"task_id": r["task_id"], "label": LABEL, "family": "대형 VLM", "name": "GPT-6-Luna + PDF",
                     "format": m["format"], "language": m["language"], "domain": m["domain"], "cost_R": r["cost"],
                     "cost_T": cost_T[r["task_id"]], "ratio": r["cost"] / max(1, cost_T[r["task_id"]]),
                     "from_empty": bool(r.get("from_empty_document")),
                     "ssim_blur": v.get("ssim_blur") if ok else None,
                     "ssim_blur_native_pages": (v["ssim_blur"] * max(v["pages_native"], v["pages_recon"]) / max(1, v["pages_native"])) if ok else None,
                     "extra_pages": (v["pages_recon"] > v["pages_native"]) if ok else None, "text_cer": v.get("text_cer") if ok else None})
    return rows


def _boot_ci(groups: dict, fn, seed: int, b: int = 2000) -> list:
    keys = sorted(groups)
    rnd = random.Random(seed)
    vals = sorted(v for v in (fn([it for k in (rnd.choice(keys) for _ in keys) for it in groups[k]]) for _ in range(b)) if v is not None)
    return [round(vals[int(0.025 * len(vals))], 3), round(vals[int(0.975 * len(vals)) - 1], 3)] if vals else [None, None]


def _med(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 4) if xs else None


def _label_stats(items: list[dict]) -> dict:
    vis = [x for x in items if x["ssim_blur_native_pages"] is not None]
    return {"tasks": len(items), "reconstruction_ok": round(1 - sum(bool(x["from_empty"]) for x in items) / max(1, len(items)), 4),
            "ratio_median": _med([x["ratio"] for x in items]),
            "ratio_of_sums": round(sum(x["cost_R"] for x in items) / max(1, sum(x["cost_T"] for x in items)), 3),
            "ssim_median": _med([x["ssim_blur_native_pages"] for x in vis]),
            "extra_pages_share": round(sum(bool(x["extra_pages"]) for x in vis) / max(1, len(vis)), 4)}


def _paired(pairs: list[tuple[dict, dict]], seed: int) -> dict:
    """pairs: (main run row, this condition's row) of the same task. Per-task factor q = ratio_pdf / ratio_main."""
    from scipy.stats import wilcoxon
    if not pairs:
        return {"tasks": 0}
    q = [b["ratio"] / a["ratio"] for a, b in pairs]
    groups = {}
    for a, b in pairs:
        groups.setdefault(a["domain"], []).append((a, b))
    lq = [math.log(x) for x in q]
    out = {"tasks": len(pairs), "domains": len(groups),
           "main_ratio_median": _med([a["ratio"] for a, _ in pairs]), "pdf_ratio_median": _med([b["ratio"] for _, b in pairs]),
           "median_difference": round(statistics.median(b["ratio"] for _, b in pairs) - statistics.median(a["ratio"] for a, _ in pairs), 3),
           "median_difference_ci95": _boot_ci(groups, lambda xs: statistics.median(b["ratio"] for _, b in xs) - statistics.median(a["ratio"] for a, _ in xs), seed),
           "factor_median": round(statistics.median(q), 3),
           "factor_median_ci95": _boot_ci(groups, lambda xs: statistics.median(b["ratio"] / a["ratio"] for a, b in xs), seed + 1),
           "factor_geomean": round(math.exp(statistics.mean(lq)), 3),
           "shorter_10pct": round(sum(x < 0.9 for x in q) / len(q), 4), "longer_10pct": round(sum(x > 1 / 0.9 for x in q) / len(q), 4),
           "wilcoxon_p": float(wilcoxon(lq).pvalue) if any(abs(v) > 1e-12 for v in lq) else 1.0}
    vp = [(a, b) for a, b in pairs if a["ssim_blur_native_pages"] is not None and b["ssim_blur_native_pages"] is not None]
    if vp:
        sv = [(a["ssim_blur_native_pages"], b["ssim_blur_native_pages"]) for a, b in vp]
        cer = [(float(a["text_cer"]), float(b["text_cer"])) for a, b in vp if a.get("text_cer") not in (None, "", "None")
               and b.get("text_cer") not in (None, "", "None")]
        out["ssim"] = {"tasks": len(sv), "main_median": _med([a for a, _ in sv]), "pdf_median": _med([b for _, b in sv]),
                       "difference_median": _med([b - a for a, b in sv]), "pdf_higher": round(sum(b > a for a, b in sv) / len(sv), 4),
                       "text_cer_main_median": _med([a for a, _ in cer]), "text_cer_pdf_median": _med([b for _, b in cer]),
                       "text_cer_pdf_lower": round(sum(b < a for a, b in cer) / max(1, len(cer)), 4),
                       "extra_pages_main": round(sum(bool(a["extra_pages"]) for a, _ in vp) / len(vp), 4),
                       "extra_pages_pdf": round(sum(bool(b["extra_pages"]) for _, b in vp) / len(vp), 4)}
    return out


def _unit_report(unit: str) -> dict:
    from formbench.experiment.report import gap_position
    rows = _rows(unit)
    by_label = {}
    for x in rows:
        by_label.setdefault(x["label"], []).append(x)
    labels = {lab: _label_stats(items) | {"by_format": {f: _label_stats([x for x in items if x["format"] == f])
                                                       for f in sorted({x["format"] for x in items})}}
              for lab, items in by_label.items()}
    main = {x["task_id"]: x for x in by_label.get(BASE, [])}
    cond = {x["task_id"]: x for x in by_label.get(LABEL, [])}
    common = sorted(set(main) & set(cond))
    pairs = [(main[t], cond[t]) for t in common]
    paired = {"all": _paired(pairs, 20261020)}
    for i, f in enumerate(sorted({a["format"] for a, _ in pairs})):
        paired[f] = _paired([(a, b) for a, b in pairs if a["format"] == f], 20261021 + 10 * i)
    paired["both_reconstructed"] = _paired([(a, b) for a, b in pairs if not a["from_empty"] and not b["from_empty"]], 20261040)
    # the four methods and both GPT conditions on the tasks that have the new condition (ranking by median ratio)
    tasks = set(cond)
    ranking = sorted(((lab, v) for lab, items in by_label.items() if (v := _med([x["ratio"] for x in items if x["task_id"] in tasks])) is not None),
                     key=lambda kv: kv[1])
    gap_rows = [x for x in rows if x["task_id"] in tasks]
    return {"unit": unit, "main_run": RUNS[unit][0], "tasks_with_condition": len(cond), "labels": labels, "paired": paired,
            "ranking_on_condition_tasks": ranking, "gap_position": gap_position(gap_rows)}


def _usage() -> dict:
    recs = [json.load(open(f)) for f in sorted(glob.glob(str(path("data", obs_name(), "*", "recon", f"{LABEL}.json"))))]
    base = {}
    for f in glob.glob(str(path("data", obs_name(), "*", "recon", f"{BASE}.json"))):
        r = json.load(open(f))
        base[r["task_id"]] = r
    fmt = {c["task_id"]: c["format"] for c in G.all_tasks()}
    ok = [r for r in recs if r["status"] == "ok"]
    bok = [base[r["task_id"]] for r in ok if base.get(r["task_id"], {}).get("status") == "ok"]
    bst = {t: r.get("status") for t, r in base.items()}
    out = {"records": len(recs), "ok": len(ok), "failed": len(recs) - len(ok),
           "main_ok": sum(1 for r in recs if bst.get(r["task_id"]) == "ok"),
           "both_ok": sum(1 for r in ok if bst.get(r["task_id"]) == "ok"),
           "only_pdf_ok": sum(1 for r in ok if bst.get(r["task_id"]) != "ok"),
           "only_main_ok": sum(1 for r in recs if r["status"] != "ok" and bst.get(r["task_id"]) == "ok"),
           "both_failed": sum(1 for r in recs if r["status"] != "ok" and bst.get(r["task_id"]) != "ok"),
           "rerun_after_outage": len(OUTAGE.read_text().split()) if OUTAGE.exists() else 0,
           "rng_valid": sum(1 for r in ok if r.get("rng_valid")),
           "tokens_median": _med([r.get("tokens") for r in ok]), "tokens_sum": sum(r.get("tokens") or 0 for r in ok),
           "main_tokens_median_same_tasks": _med([r.get("tokens") for r in bok]),
           "seconds_median": _med([r.get("elapsed_s") for r in ok]), "main_seconds_median_same_tasks": _med([r.get("elapsed_s") for r in bok]),
           "commands_median": _med([r.get("commands") for r in ok]),
           "read_pdf_share": round(sum(1 for r in ok if (r.get("pdf_commands") or 0) > 0) / max(1, len(ok)), 4),
           "python_share": round(sum(1 for r in ok if (r.get("python_commands") or 0) > 0) / max(1, len(ok)), 4),
           "pdf_commands_median": _med([r.get("pdf_commands") for r in ok]),
           "first_started": min((r["started_at"] for r in recs), default=None), "last_started": max((r["started_at"] for r in recs), default=None)}
    for f in ("docx", "hwpx", "odt"):
        sub = [r for r in recs if fmt.get(r["task_id"]) == f]
        out[f"by_format_{f}"] = {"records": len(sub), "ok": sum(r["status"] == "ok" for r in sub),
                                 "read_pdf_share": round(sum(1 for r in sub if (r.get("pdf_commands") or 0) > 0) / max(1, len(sub)), 4)}
    return out


def _md(s: dict) -> str:
    L = ["# GPT-6-Luna: 디지털 PDF 추가 조건 (current 보충)", "",
         f"생성 {s['created_at']}. 본 실행(쪽 이미지 + ODF 명세) 대비 쪽 이미지를 만든 디지털 PDF(텍스트 층 포함)를 추가로 준 조건. 같은 과제끼리 짝지어 비교.", ""]
    u = s["usage"]
    L += [f"* 복원 성공 {u['ok']}/{u['records']} (본 실행 {u['main_ok']}/{u['records']}; 둘 다 성공 {u['both_ok']}, PDF 조건만 성공 {u['only_pdf_ok']},"
          f" 본 실행만 성공 {u['only_main_ok']}, 둘 다 실패 {u['both_failed']}), RELAX NG 유효 {u['rng_valid']}.",
          f"* Codex 서비스 장애(연결 실패)로 답 없이 끝난 {u['rerun_after_outage']}개 과제는 결과가 아니므로 기록을 옮기고 다시 실행함"
          " (data/logs/current_gpt_pdf_env_failures/).",
          f"* PDF를 읽은 답 {u['read_pdf_share'] * 100:.1f}% (python3 사용 {u['python_share'] * 100:.1f}%), 토큰 중앙값 {u['tokens_median']} (본 실행 같은 과제 {u['main_tokens_median_same_tasks']}),"
          f" 시간 중앙값 {u['seconds_median']}초 (본 실행 {u['main_seconds_median_same_tasks']}초).", ""]
    for unit in ("klm", "actions"):
        r = s.get(unit)
        if not r:
            continue
        L += [f"## {'편집 시간 (KLM 초)' if unit == 'klm' else '동작 수'} — {r['main_run']} 기준", "",
              "| 층 | 과제 | 본 실행 배율 | PDF 추가 배율 | 차이 (95% 구간) | 과제별 배수 중앙값 (95% 구간) | 10%↑ 단축 | 10%↑ 증가 | Wilcoxon p |",
              "|---|---|---|---|---|---|---|---|---|"]
        for k, p in r["paired"].items():
            if not p.get("tasks"):
                continue
            L.append(f"| {k} | {p['tasks']} | {p['main_ratio_median']:.2f} | {p['pdf_ratio_median']:.2f} | {p['median_difference']:+.2f} "
                     f"({p['median_difference_ci95'][0]:+.2f}–{p['median_difference_ci95'][1]:+.2f}) | {p['factor_median']:.2f} "
                     f"({p['factor_median_ci95'][0]:.2f}–{p['factor_median_ci95'][1]:.2f}) | {p['shorter_10pct'] * 100:.0f}% | "
                     f"{p['longer_10pct'] * 100:.0f}% | {p['wilcoxon_p']:.2g} |")
        L += ["", "| 층 (둘 다 복원) | 과제 | 시각 유사도 중앙값 | 문자 오류율 중앙값 | 쪽 넘침 |", "|---|---|---|---|---|"]
        for k, p in r["paired"].items():
            v = p.get("ssim")
            if v:
                L.append(f"| {k} | {v['tasks']} | {v['main_median']:.3f} → {v['pdf_median']:.3f} | {v['text_cer_main_median']:.3f} → "
                         f"{v['text_cer_pdf_median']:.3f} | {v['extra_pages_main'] * 100:.1f}% → {v['extra_pages_pdf'] * 100:.1f}% |")
        L.append("")
        L += ["방식별 배율 중앙값(이 조건이 있는 과제): " + ", ".join(f"{lab} {v:.2f}" for lab, v in r["ranking_on_condition_tasks"]), ""]
        g = r["gap_position"]
        if g:
            L += ["천장–바닥 상대 위치 (양성 대조군 0%, 쪽 이미지 100%):", "", "| 층 | " + " | ".join(sorted(g["all"]["methods"])) + " |",
                  "|---|" + "---|" * len(g["all"]["methods"])]
            for k, v in g.items():
                L.append(f"| {k} | " + " | ".join(
                    f"{v['methods'][lab]['position'] * 100:.0f}% ({v['methods'][lab]['ci95'][0] * 100:.0f}–{v['methods'][lab]['ci95'][1] * 100:.0f})"
                    if lab in v["methods"] else "–" for lab in sorted(g["all"]["methods"])) + " |")
            L.append("")
    return "\n".join(L) + "\n"


def report() -> None:
    s = {"created_at": utcnow(), "label": LABEL, "main_label": BASE, "prompt": str(PROMPT.relative_to(path())),
         "prompt_sha256": sha256_bytes(PROMPT.read_bytes()), "usage": _usage()}
    for unit in ("klm", "actions"):
        if glob.glob(str(path("data", "runs", RUNS[unit][1], LABEL, "*.json"))):
            s[unit] = _unit_report(unit)
            rows = [x for x in _rows(unit) if x["label"] in (BASE, LABEL)]
            REPORT.mkdir(parents=True, exist_ok=True)
            with open(REPORT / f"rows_{unit}.csv", "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=list(rows[-1]))
                w.writeheader()
                w.writerows({k: x.get(k) for k in rows[-1]} for x in rows)
    REPORT.mkdir(parents=True, exist_ok=True)
    write_json(REPORT / "summary.json", s)
    (REPORT / "report.md").write_text(_md(s), encoding="utf-8")
    print((REPORT / "report.md").read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "visual", "cost", "report", "prompt"])
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--tasks", default="", help="comma-separated task ids (pilot)")
    ap.add_argument("--units", choices=["klm", "actions"], default="klm", help="cost: KLM seconds or action counts")
    a = ap.parse_args()
    if a.cmd == "prompt":                                # show the prompt of a chunk of a long document
        print(prompt_text(1, 8, 12))
        return 0
    if a.cmd == "run":
        ts = G.all_tasks()
        if a.tasks:
            want = set(a.tasks.split(","))
            ts = [c for c in ts if c["task_id"] in want or c["task_id"].replace("~", "__") in want]
        if a.limit:
            ts = ts[: a.limit]
        print(f"# {len(ts)} tasks, model {G.MODEL}, {G.codex_version()}", flush=True)
        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            for f in [ex.submit(job, c, lambda s: print(s, flush=True)) for c in ts]:
                f.result()
    elif a.cmd == "visual":
        visual(min(a.workers, 3))
    elif a.cmd == "cost":
        cost(min(a.workers, 3), a.units)
    else:
        report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
