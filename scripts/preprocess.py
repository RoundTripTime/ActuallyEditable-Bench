"""Experiment current (user decision 2026-10-19): the original AND the example are both preprocessed to ODT first, so that the
original's own word processor no longer takes part in the experiment (inputs, visual reference and edit time are all
ODT documents rendered / laid out by one common pipeline). Protocol: docs/experiment.md.

Conversion routes (candidates; the same route is always used for the template and the example of one task):
  lo    LibreOffice's own import (DOCX filter; HWPX: H2Orestart, through DOCX when LibreOffice cannot store the HWPX
        directly) -- the edit route, files copied from data/source_documents (oracle__template_odt.odt, example.odt)
  word  DOCX only: Microsoft Word opens the DOCX and saves OpenDocument Text (wdFormatOpenDocumentText)
  hwp   HWPX only: Hancom Hangul opens the HWPX and saves ODT (writer "ODF")
  copy  ODT originals: the file itself (lossless)
Selection (per task; rule revised 2026-10-19 before any reconstruction result was looked at, applied by `reselect`):
default `lo`; the application's own export replaces it only when it is a Pareto improvement: conversion loss (edit time in
KLM seconds, edit cost model, to turn the converted template back into the native template + the same for the example) lower
by more than 1 % AND visual similarity of the converted template (LibreOffice rendering vs native rendering) not lower
by more than 0.02. Neither criterion alone works (see choose()).
Output: data/experiment_documents/<task>/{template.odt, example.odt, prep.json, native.pdf (LibreOffice rendering of template.odt),
native.json} and data/experiment_documents/<task>/prep/<doc>.<route>.{odt,json,lo.pdf}. Resumable.
Usage: python scripts/preprocess.py lo | apps [--app word|hwp] | score [--workers N] [--formats odt] | render | oracle | inputs
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from formbench.util import path, sha256_bytes, utcnow, write_json  # noqa: E402

SOURCE_DOCUMENTS = Path(path("data", "source_documents"))
EXPERIMENT_DOCUMENTS = Path(path("data", "experiment_documents"))
ROUTES = {"docx": ["lo", "word"], "hwpx": ["lo", "hwp"], "odt": ["copy"]}
DOCS = ("template", "example")


def contracts() -> list[dict]:
    return [json.load(open(f)) for f in sorted(glob.glob(str(path("data", "observed_contracts", "doc-*.json"))))]


def tdir(c: dict) -> str:
    return c["task_id"].replace("~", "__")


def native_bytes(c: dict, doc: str) -> bytes:
    from formbench.collection.discovery import blob_path
    from formbench.util import DB
    return blob_path(DB().get_doc(c["template_id" if doc == "template" else "completed_id"])["sha256"]).read_bytes()


def put(c: dict, doc: str, route: str, odt: bytes | None, info: dict) -> None:
    p = EXPERIMENT_DOCUMENTS / tdir(c) / "prep"
    p.mkdir(parents=True, exist_ok=True)
    rec = {"task_id": c["task_id"], "doc": doc, "route": route, "format": c["format"], "created_at": utcnow(), **info}
    if odt:
        (p / f"{doc}.{route}.odt").write_bytes(odt)
        rec.update(status="ok", sha256=sha256_bytes(odt))
    else:
        rec.setdefault("status", "failed")
    write_json(p / f"{doc}.{route}.json", rec)


def done(c: dict, doc: str, route: str) -> bool:
    return (EXPERIMENT_DOCUMENTS / tdir(c) / "prep" / f"{doc}.{route}.json").exists()


def cmd_lo() -> None:
    """The edit route: copy the files edit already built (same LibreOffice / H2Orestart route for both documents)."""
    n = 0
    for c in contracts():
        d3 = SOURCE_DOCUMENTS / tdir(c)
        route = "copy" if c["format"] == "odt" else "lo"
        src = {"template": d3 / "recon" / "oracle__template_odt.odt", "example": d3 / "example.odt"}
        meta = {"template": d3 / "recon" / "oracle__template_odt.json", "example": d3 / "example_odt.json"}
        for doc in DOCS:
            if done(c, doc, route):
                continue
            m = json.load(open(meta[doc]))if meta[doc].exists() else {}
            via = (m.get("info") or {}).get("route") or m.get("route") or ("copy" if route == "copy" else "libreoffice_import")
            put(c, doc, route, src[doc].read_bytes() if src[doc].exists() else None, {"via": via, "from": str(src[doc].relative_to(path()))})
            n += 1
    print(f"# lo/copy: {n} files", flush=True)


def cmd_apps(only: str | None) -> None:
    """Word (DOCX) and Hangul (HWPX) save the document as ODT themselves. Serial (one Windows application at a time)."""
    from formbench.formats.native_render import app_pdf_to_odt
    for app, fmt in (("word", "docx"), ("hwp", "hwpx")):
        if only and app != only:
            continue
        todo = [(c, doc) for c in contracts() if c["format"] == fmt for doc in DOCS if not done(c, doc, app)]
        print(f"# {app}: {len(todo)} documents", flush=True)
        k = 0
        for i in range(0, len(todo), 20):                      # one call per 20 files: the registry set-up runs once per call
            chunk = todo[i:i + 20]
            t0 = time.monotonic()
            for _ in range(4):                                  # our previous instance may still be shutting down
                res = app_pdf_to_odt(app, [(native_bytes(c, doc), f"f{j}") for j, (c, doc) in enumerate(chunk)], per_file_s=240, src_ext=fmt)
                if all(r["status"] != "blocked" for r in res.values()):
                    break
                time.sleep(20)
            for j, (c, doc) in enumerate(chunk):
                r = res[f"f{j}"]
                k += 1
                if r["status"] == "blocked":                     # the user has the application open: stop, resume later
                    print(json.dumps({"task_id": c["task_id"], "app": app, "status": "blocked", "reason": r.get("reason")}), flush=True)
                    return
                put(c, doc, app, r.get("odt"), {"status": r["status"], "reason": r.get("reason", ""), "via": r.get("app"),
                                                "app_seconds": r.get("s")})
                print(json.dumps({"n": f"{k}/{len(todo)}", "task_id": c["task_id"][:24], "doc": doc, "app": app, "status": r["status"],
                                  "reason": (r.get("reason") or "")[:100]}, ensure_ascii=False), flush=True)
            print(f"# chunk {i // 20 + 1}: {len(chunk)} in {time.monotonic() - t0:.0f}s", flush=True)


def _loss(args) -> dict:
    """Conversion loss of every candidate route of one task (KLM seconds; edit cost model, own internal layout engine)."""
    cpath, routes = args
    os.environ["FORMBENCH_COST_UNITS"] = "klm"
    from formbench.completion.sessions import open_session
    from formbench.editing.cost import document_cost
    c = json.load(open(cpath))
    p = EXPERIMENT_DOCUMENTS / tdir(c) / "prep"
    out = {}
    nat = {doc: open_session(native_bytes(c, doc), c["format"]) for doc in DOCS}
    for route in routes:
        f = {doc: p / f"{doc}.{route}.odt" for doc in DOCS}
        if not all(x.exists() for x in f.values()):
            out[route] = {"status": "failed"}
            continue
        r = {"status": "ok"}
        for doc in DOCS:
            try:
                r[f"loss_{doc}"] = round(document_cost(open_session(f[doc].read_bytes(), "odt"), nat[doc], lang=c.get("language"))["cost"], 2)
            except Exception as e:  # noqa: BLE001
                r.update(status="failed", reason=f"{doc}: {type(e).__name__}: {e}"[:200])
        out[route] = r
    return {"task": tdir(c), "routes": out}


RULE = ("default lo (copy for ODT); the application's own ODT export replaces it only when it lowers the conversion loss "
        "(loss_template+loss_example, KLM s) by more than 1% AND keeps visual_template within 0.02 of lo's (Pareto improvement)")


def choose(cand: list[tuple[str, float, float]]) -> str:
    """cand: (route, loss, visual_template). Default: LibreOffice's own import (open source, the edit route). The
    application's export replaces it only if it is better on one criterion without being worse on the other: the two
    criteria conflict in both directions (Word's ODT export of Japanese documents with a character grid keeps the
    structure but renders with widely spread characters in LibreOffice; Hangul's ODT export can look closer while putting
    a page into one text box), so neither alone can decide."""
    by = {x[0]: x for x in cand}
    base = by.get("copy") or by.get("lo")
    if base is None:
        return min(cand, key=lambda x: x[1])[0]
    better = [x for x in cand if x is not base and x[1] < base[1] * 0.99 and x[2] >= base[2] - 0.02]
    return min(better, key=lambda x: x[1])[0] if better else base[0]


def cmd_reselect() -> None:
    """Re-apply the selection rule to the stored candidate scores (prep.json); a task whose route changes loses every file
    derived from the old preprocessed template (rendering, inputs, reconstructions), so the later steps rebuild it."""
    n = 0
    for f in sorted(EXPERIMENT_DOCUMENTS.glob("*/prep.json")):
        j = json.load(open(f))
        if j.get("status") != "ok":
            continue
        cand = [(r, x["loss_template"] + x["loss_example"], x.get("visual_template") or 0.0) for r, x in j["routes"].items() if x["status"] == "ok"]
        route = choose(cand)
        if route == j["route"] and j.get("rule") == RULE:
            continue
        d = f.parent
        if route != j["route"]:
            for x in ("native.pdf", "native.json"):
                (d / x).unlink(missing_ok=True)
            for x in ("inputs", "recon"):
                shutil.rmtree(d / x, ignore_errors=True)
            for doc in DOCS:
                shutil.copyfile(d / "prep" / f"{doc}.{route}.odt", d / f"{doc}.odt")
            n += 1
        j.update(previous_route=j["route"], route=route, rule=RULE, chosen_at=utcnow())
        write_json(f, j)
    print(f"# reselect: {n} tasks changed route", flush=True)


def cmd_score(workers: int, formats: set[str] | None = None) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import visual_helpers
    cs = [c for c in contracts() if formats is None or c["format"] in formats]
    # 1) visual similarity of each converted template to the native rendering of the template (LibreOffice rendering)
    odts = [str(EXPERIMENT_DOCUMENTS / tdir(c) / "prep" / f"template.{r}.odt") for c in cs for r in ROUTES[c["format"]]
            if (EXPERIMENT_DOCUMENTS / tdir(c) / "prep" / f"template.{r}.odt").exists()]
    todo = [o for o in odts if not Path(o[:-4] + ".lo.pdf").exists()]
    print(f"# render {len(todo)} converted templates", flush=True)
    for i in range(0, len(todo), 25):
        visual_helpers.render_batch(todo[i:i + 25])
    vis = {}
    for o in odts:
        lo, nat = Path(o[:-4] + ".lo.pdf"), SOURCE_DOCUMENTS / Path(o).parent.parent.name / "native.pdf"
        vis[o] = _ssim(nat.read_bytes(), lo.read_bytes()) if lo.exists() else None
    # 2) conversion loss in edit time
    jobs = [(str(path("data", "observed_contracts", f"{tdir(c)}.json")), ROUTES[c["format"]]) for c in cs
            if not (EXPERIMENT_DOCUMENTS / tdir(c) / "prep.json").exists()]
    print(f"# score {len(jobs)} tasks", flush=True)
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for k, fut in enumerate(as_completed([ex.submit(_loss, j) for j in jobs]), 1):
            r = fut.result()
            d = EXPERIMENT_DOCUMENTS / r["task"]
            c = json.load(open(path("data", "observed_contracts", f"{r['task']}.json")))
            cand = []
            for route, x in r["routes"].items():
                x["visual_template"] = vis.get(str(d / "prep" / f"template.{route}.odt"))
                if x["status"] == "ok":
                    cand.append((route, x["loss_template"] + x["loss_example"], x["visual_template"] or 0.0))
            if not cand:
                write_json(d / "prep.json", {"task_id": c["task_id"], "status": "failed", "routes": r["routes"]})
                print(json.dumps({"task": r["task"][:24], "status": "failed"}), flush=True)
                continue
            route = choose(cand)
            for doc in DOCS:
                shutil.copyfile(d / "prep" / f"{doc}.{route}.odt", d / f"{doc}.odt")
            write_json(d / "prep.json", {"task_id": c["task_id"], "format": c["format"], "status": "ok", "route": route,
                                         "rule": RULE,
                                         "routes": r["routes"], "chosen_at": utcnow()})
            if k % 20 == 0:
                print(f"# scored {k}/{len(jobs)}", flush=True)


def _ssim(a: bytes, b: bytes) -> float:
    """Blurred SSIM averaged over the native template's pages (edit visual-similarity definition, pages added by the
    conversion are not averaged)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import visual_helpers
    from formbench.formats.renderers import pdf_to_pngs
    pa, pb = pdf_to_pngs(a, visual_helpers.DPI), pdf_to_pngs(b, visual_helpers.DPI)
    return round(sum(visual_helpers.ssim_blur(pa[i], pb[i]) if i < len(pb) else 0.0 for i in range(len(pa))) / len(pa), 4)


def cmd_render() -> None:
    """native.pdf of current = LibreOffice rendering of the preprocessed template (the reconstruction input and the visual
    reference). The converted template's rendering from `score` is reused (same LibreOffice, same fonts)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import visual_helpers
    todo = []
    for c in contracts():
        d = EXPERIMENT_DOCUMENTS / tdir(c)
        if not (d / "template.odt").exists() or (d / "native.pdf").exists():
            continue
        todo.append(d)
    print(f"# render {len(todo)} templates", flush=True)
    import pymupdf
    for d in todo:
        route = json.load(open(d / "prep.json"))["route"]
        lo = d / "prep" / f"template.{route}.lo.pdf"
        if not lo.exists():
            visual_helpers.render_batch([str(d / "prep" / f"template.{route}.odt")])
        if not lo.exists():
            print(json.dumps({"task": d.name[:24], "status": "render_failed"}), flush=True)
            continue
        b = lo.read_bytes()
        (d / "native.pdf").write_bytes(b)
        write_json(d / "native.json", {"renderer": visual_helpers_renderer(), "format": "odt", "pages": len(pymupdf.open(stream=b)),
                                       "sha256": sha256_bytes(b), "from": f"template.odt (route {route})", "rendered_at": utcnow()})


def cmd_inputs() -> None:
    """Reconstruction inputs from native.pdf, exactly as edit (scripts/reconstruction_helpers.py make_inputs: vector_pdf, png_150, ...)."""
    os.environ["FORMBENCH_OBS"] = "experiment_documents"
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import reconstruction_helpers
    n = 0
    for c in contracts():
        d = EXPERIMENT_DOCUMENTS / tdir(c)
        if (d / "native.pdf").exists() and not (d / "inputs" / "inputs.json").exists():
            reconstruction_helpers.make_inputs(d)
            reconstruction_helpers.make_inputs(d)                       # second call adds image_pdf_150 (edit behaviour)
            n += 1
    print(f"# inputs: {n}", flush=True)


def cmd_oracle() -> None:
    """current positive control: the preprocessed template itself (no reconstruction). It checks that the common pipeline is
    exact: visual similarity 1 and edit time ratio 1 by construction."""
    n = 0
    for c in contracts():
        d = EXPERIMENT_DOCUMENTS / tdir(c)
        rd = d / "recon"
        if not (d / "template.odt").exists() or (rd / "oracle__template_odt.json").exists():
            continue
        rd.mkdir(exist_ok=True)
        b = (d / "template.odt").read_bytes()
        (rd / "oracle__template_odt.odt").write_bytes(b)
        write_json(rd / "oracle__template_odt.json", {"task_id": c["task_id"], "input": "template", "baseline": "oracle",
                                                      "label": "oracle__template_odt", "status": "ok", "sha256": sha256_bytes(b),
                                                      "created_at": utcnow(), "info": {"route": "copy of the preprocessed template"}})
        n += 1
    print(f"# oracle: {n}", flush=True)


def visual_helpers_renderer() -> str:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import reconstruction_helpers
    return reconstruction_helpers.lo_version() or "LibreOffice"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["lo", "apps", "score", "render", "oracle", "inputs", "reselect"])
    ap.add_argument("--app", choices=["word", "hwp"])
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--formats", default="", help="score only these formats (comma-separated), e.g. odt while the apps run")
    a = ap.parse_args()
    {"lo": cmd_lo, "apps": lambda: cmd_apps(a.app), "score": lambda: cmd_score(a.workers, set(a.formats.split(",")) if a.formats else None), "render": cmd_render, "oracle": cmd_oracle, "inputs": cmd_inputs, "reselect": cmd_reselect}[a.cmd]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
