"""Experiment edit pilot (user request 2026-10-18): GPT-6-Luna writes the ODT itself instead of HTML -> LibreOffice -> ODT.

Question: does the LibreOffice HTML import (used for GPT-6-Luna, Docling and MinerU2.5) change the results? The same model,
input (png_150 page images, one request per document) and rules, while the prompt
(configs/prompts/vlm_form_odt_v1.txt) asks for a single-file OpenDocument text document (ODF 1.4 Part 3 §3.1.2
<office:document>) and attaches the official OASIS ODF 1.4 specification (tools/odf_spec/v1.4: Part 2, Part 3, RELAX NG
schema) in the Codex working directory; the read-only sandbox lets the model read them and nothing else.
The answer is packed into an ODT package without any office application (pack_fodt: the Part 2 split of the same XML
into content.xml / styles.xml / meta.xml / settings.xml, a lossless repackaging), so no LibreOffice conversion is involved.

Sample: 24 tasks of at most 8 pages (one request, like the HTML route for those tasks), stratified by format x language
(DOCX ja 8, DOCX en 6, ODT zh 5, HWPX ko 5), first tasks in sha256(task_id) order per stratum.
Label: png_150__gpt6luna_odt in data/source_documents/<task>/recon/ (.fodt raw answer, .odt package, .json record).

Usage:
  python scripts/reconstruct_gpt.py run [--limit N] [--workers 2]   reconstruct (Codex, resumable)
  python scripts/reconstruct_gpt.py eval                           visual similarity + editing time for both GPT labels
  python scripts/reconstruct_gpt.py report                         paired comparison -> reports/experiments/gpt-pilot/
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import importlib.util
import io
import json
import os
import re
import statistics as st
import subprocess
import sys
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

os.environ.setdefault("FORMBENCH_COST_UNITS", "klm")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from formbench.util import obs_name, path, sha256_bytes, utcnow, write_json  # noqa: E402

MODEL = "gpt-6-luna"
EFFORT = "high"
LABEL = "png_150__gpt6luna_odt"
BASE_LABEL = "png_150__gpt6luna"
PROMPT = Path(path("configs", "prompts", "vlm_form_odt_v1.txt"))
SPEC = Path(path("tools", "odf_spec", "v1.4"))
SPEC_FILES = ["OpenDocument-v1.4-os-part3-schema.txt", "OpenDocument-v1.4-os-part2-packages.txt", "OpenDocument-v1.4-schema.rng"]
STRATA = {("docx", "ja"): 8, ("docx", "en"): 6, ("odt", "zh"): 5, ("hwpx", "ko"): 5}
MAX_PAGES = 8
RUN_ID = "gpt-pilot"
REPORT = Path(path("reports", "experiments", RUN_ID))
LIMIT_RE = re.compile(r"usage limit|rate limit|too many requests|\b429\b|quota|limit reached|try again (?:in|at|later)", re.I)
HERE = Path(__file__).resolve().parent
NS_OFFICE = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"
NS_MANIFEST = "urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"
MIME = "application/vnd.oasis.opendocument.text"


def _mod(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def task_dir(c: dict) -> Path:
    return Path(path("data", obs_name(), c["task_id"].replace("~", "__")))


def pages(d: Path) -> list[Path]:
    return sorted((d / "inputs").glob("png_150_p*.png"), key=lambda p: int(p.stem.rsplit("_p", 1)[1]))


def sample() -> list[dict]:
    cs = [json.load(open(f)) for f in sorted(glob.glob(str(path("data", "observed_contracts", "doc-*.json"))))]
    out = []
    for (fmt, lang), k in STRATA.items():
        pool = [c for c in cs if c["format"] == fmt and c.get("language") == lang and 1 <= len(pages(task_dir(c))) <= MAX_PAGES]
        pool.sort(key=lambda c: hashlib.sha256(c["task_id"].encode()).hexdigest())
        out += pool[:k]
    return out


def all_tasks() -> list[dict]:
    return [json.load(open(f)) for f in sorted(glob.glob(str(path("data", "observed_contracts", "doc-*.json"))))]


# ---------------------------------------------------------------- packing (no office application)
def pack_fodt(xml: bytes) -> bytes:
    """Single-file ODF text document (<office:document>) -> ODT package (ODF Part 2): the same elements split into
    content.xml (scripts, font faces, automatic styles, body), styles.xml (font faces, styles, automatic styles,
    master styles), meta.xml and settings.xml, plus mimetype and META-INF/manifest.xml. Automatic styles go to both
    files (each file has its own automatic-style scope), so every style reference keeps resolving."""
    from lxml import etree
    root = etree.fromstring(xml, etree.XMLParser(resolve_entities=False, huge_tree=True))
    if etree.QName(root).localname != "document" or etree.QName(root).namespace != NS_OFFICE:
        raise ValueError(f"root is {root.tag}, not office:document")
    version = root.get(f"{{{NS_OFFICE}}}version", "1.4")
    kids = {etree.QName(e).localname: e for e in root if isinstance(e.tag, str)}
    if "body" not in kids:
        raise ValueError("no office:body")

    def part(name: str, children: list[str]) -> bytes:
        el = etree.Element(f"{{{NS_OFFICE}}}{name}", nsmap=root.nsmap)
        el.set(f"{{{NS_OFFICE}}}version", version)
        for ch in children:
            if ch in kids:
                el.append(etree.fromstring(etree.tostring(kids[ch])))
        return etree.tostring(el, xml_declaration=True, encoding="UTF-8")

    files = {"content.xml": part("document-content", ["scripts", "font-face-decls", "automatic-styles", "body"]),
             "styles.xml": part("document-styles", ["font-face-decls", "styles", "automatic-styles", "master-styles"])}
    if "meta" in kids:
        files["meta.xml"] = part("document-meta", ["meta"])
    if "settings" in kids:
        files["settings.xml"] = part("document-settings", ["settings"])
    man = etree.Element(f"{{{NS_MANIFEST}}}manifest", nsmap={"manifest": NS_MANIFEST})
    man.set(f"{{{NS_MANIFEST}}}version", version)
    for fp, mt in [("/", MIME)] + [(f, "text/xml") for f in files]:
        e = etree.SubElement(man, f"{{{NS_MANIFEST}}}file-entry")
        e.set(f"{{{NS_MANIFEST}}}full-path", fp)
        e.set(f"{{{NS_MANIFEST}}}media-type", mt)
        if fp == "/":
            e.set(f"{{{NS_MANIFEST}}}version", version)
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zipfile.ZipInfo("mimetype"), MIME, compress_type=zipfile.ZIP_STORED)
        for f, data in files.items():
            z.writestr(f, data)
        z.writestr("META-INF/manifest.xml", etree.tostring(man, xml_declaration=True, encoding="UTF-8"))
    return b.getvalue()


REF_ATTRS = {"style-name", "parent-style-name", "next-style-name", "page-layout-name", "master-page-name", "list-style-name",
             "data-style-name", "text-style-name", "default-cell-style-name", "default-style-name", "apply-style-name",
             "visited-style-name", "style-name-ref", "font-name", "font-name-asian", "font-name-complex"}


def merge_fodt(parts: list[str]) -> str:
    """Several single-file ODF answers (page chunks of one document) -> one: the first answer keeps its styles and master
    pages; the styles and font faces of later answers are renamed with a chunk prefix (c2_, c3_, ...) and added, their
    body content is appended after a paragraph with fo:break-before="page" (as the HTML route inserts a page-break div);
    references to master pages of later answers are dropped (their pages use the first master page)."""
    from lxml import etree
    ns = {"office": NS_OFFICE, "style": "urn:oasis:names:tc:opendocument:xmlns:style:1.0",
          "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0", "fo": "urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0"}
    P = etree.XMLParser(resolve_entities=False, huge_tree=True)
    docs = [etree.fromstring(x.encode("utf-8"), P) for x in parts]
    root = docs[0]

    def sect(doc, name):
        e = doc.find(f"office:{name}", ns)
        if e is None:
            e = etree.SubElement(doc, f"{{{NS_OFFICE}}}{name}")
        return e
    text0 = root.find("office:body/office:text", ns)
    auto0 = sect(root, "automatic-styles")
    auto0.append(etree.fromstring(f'<style:style xmlns:style="{ns["style"]}" xmlns:fo="{ns["fo"]}" style:name="merge_pb" style:family="paragraph">'
                                  f'<style:paragraph-properties fo:break-before="page"/></style:style>'))
    for k, doc in enumerate(docs[1:], start=2):
        pre = f"c{k}_"
        masters = {e.get(f"{{{ns['style']}}}name") for e in doc.iterfind(".//style:master-page", ns)}
        names = {e.get(f"{{{ns['style']}}}name") for e in doc.iter() if isinstance(e.tag, str) and e.get(f"{{{ns['style']}}}name")
                 and etree.QName(e).localname != "master-page"}
        for e in doc.iter():
            if not isinstance(e.tag, str):
                continue
            for a, v in list(e.attrib.items()):
                ln = etree.QName(a).localname
                if ln == "name" and a == f"{{{ns['style']}}}name" and v in names:
                    e.set(a, pre + v)
                elif ln == "master-page-name" and v in masters:
                    del e.attrib[a]
                elif ln in REF_ATTRS and v in names:
                    e.set(a, pre + v)
        for sec in ("font-face-decls", "styles", "automatic-styles"):
            src = doc.find(f"office:{sec}", ns)
            if src is not None:
                dst = sect(root, sec)
                for ch in list(src):
                    if isinstance(ch.tag, str) and etree.QName(ch).localname != "default-style":
                        dst.append(ch)
        body = doc.find("office:body/office:text", ns)
        pb = etree.SubElement(text0, f"{{{ns['text']}}}p")
        pb.set(f"{{{ns['text']}}}style-name", "merge_pb")
        for ch in list(body):
            if isinstance(ch.tag, str) and etree.QName(ch).localname in ("sequence-decls", "variable-decls", "user-field-decls", "forms"):
                continue
            text0.append(ch)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8").decode("utf-8")


def rng_check(xml: bytes) -> dict:
    """Validity against the official ODF 1.4 RELAX NG schema with jing -i (ID types off, as the schema needs; libxml2
    rejects valid ODF tables, so it is not used); reported, never used to reject."""
    jars = Path.home() / "opt" / "jars"
    cp = f"{jars / 'jing-20220510.jar'}:{jars / 'datatype-20220510.jar'}"
    with tempfile.NamedTemporaryFile(suffix=".fodt") as f:
        f.write(xml)
        f.flush()
        try:
            p = subprocess.run(["java", "-cp", cp, "com.thaiopensource.relaxng.util.Driver", "-i", str(SPEC / "OpenDocument-v1.4-schema.rng"),
                                f.name], capture_output=True, text=True, timeout=300)
        except Exception as e:  # noqa: BLE001
            return {"rng_valid": None, "rng_errors": [f"{type(e).__name__}: {e}"[:200]]}
    errs = [ln.split(" error: ", 1)[-1][:160] for ln in p.stdout.splitlines() + p.stderr.splitlines() if " error: " in ln]
    return {"rng_valid": p.returncode == 0, "rng_error_count": len(errs), "rng_errors": errs[:8], "validator": "jing 20220510 -i"}


# ---------------------------------------------------------------- Codex call
def codex_version() -> str:
    return subprocess.run(["codex", "--version"], capture_output=True, text=True).stdout.strip()


def call(images: list[Path]) -> dict:
    with tempfile.TemporaryDirectory(prefix="fb_codex_odt_") as td:
        (Path(td) / "odf_spec").mkdir()
        for f in SPEC_FILES:
            (Path(td) / "odf_spec" / f).write_bytes((SPEC / f).read_bytes())
        out = Path(td) / "last.xml"
        cmd = ["codex", "exec", "-m", MODEL, "-c", f'model_reasoning_effort="{EFFORT}"', "-s", "read-only", "--ephemeral",
               "--skip-git-repo-check", "-o", str(out)]
        for im in images:
            cmd += ["-i", str(im)]
        cmd += ["-"]
        t0 = time.monotonic()
        try:
            p = subprocess.run(cmd, input=PROMPT.read_text(encoding="utf-8"), capture_output=True, text=True, cwd=td, timeout=2400)
        except subprocess.TimeoutExpired:
            return {"error": "codex call exceeded 2400 s", "seconds": round(time.monotonic() - t0, 1)}
        log = (p.stdout or "") + (p.stderr or "")
        m = re.search(r"tokens used\s*\n\s*([\d,]+)", log)
        tokens = int(m.group(1).replace(",", "")) if m else None
        commands = len(re.findall(r"^exec\b|^\[[^\]]*\] exec ", log, re.M))
        ans = out.read_text(encoding="utf-8", errors="replace") if out.exists() else ""
        a, b = ans.find("<?xml"), ans.rfind("</office:document>")
        if a >= 0 and b > a:
            return {"xml": ans[a:b + len("</office:document>")], "tokens": tokens, "commands": commands,
                    "seconds": round(time.monotonic() - t0, 1), "log_tail": log[-40000:]}
        errs = [ln for ln in log.splitlines() if ln.startswith("ERROR") or "error" in ln.lower()[:20]]
        msg = " | ".join(errs)[-600:] or log[-600:]
        if LIMIT_RE.search(msg):
            return {"limit": msg}
        return {"error": msg or f"rc={p.returncode}, no ODF answer", "tokens": tokens, "seconds": round(time.monotonic() - t0, 1)}


def answer(images: list[Path], rd: Path, tag: str, log) -> tuple[dict, list]:
    """One chunk: up to 2 requests until the answer is well-formed ODF that packs; usage limits wait and retry."""
    attempts, res = [], {}
    for _ in range(2):
        while True:
            res = call(images)
            if "limit" not in res:
                break
            log(json.dumps({"event": "usage_limit", "task": rd.parent.name[:24], "message": res["limit"][:200], "at": utcnow()}))
            time.sleep(1800)
        rec_try = {k: v for k, v in res.items() if k not in ("xml", "log_tail")}
        if res.get("log_tail"):
            (rd / f"{LABEL}{tag}.try{len(attempts) + 1}.codex.log").write_text(res["log_tail"], encoding="utf-8")
        if "xml" in res:
            try:
                pack_fodt(res["xml"].encode("utf-8"))
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
    d = task_dir(c)
    rd = d / "recon"
    if (rd / f"{LABEL}.json").exists():
        return
    pg = pages(d)
    if not pg:                                            # inputs not built yet (current builds them per task): a later run does it
        return
    chunks = [pg[i:i + MAX_PAGES] for i in range(0, len(pg), MAX_PAGES)]
    parts, attempts, res = [], [], {}
    for k, ch in enumerate(chunks):
        res, att = answer(ch, rd, f".chunk{k + 1}" if len(chunks) > 1 else "", log)
        attempts += [a | {"chunk": k + 1} for a in att] if len(chunks) > 1 else att
        if "xml" not in res:
            break
        parts.append(res["xml"])
    rec = {"task_id": c["task_id"], "input": "png_150", "baseline": "gpt6luna_odt", "family": "B2-vlm-api", "started_at": utcnow(),
           "attempts": attempts, "requests": len(parts),
           "info": {"tool": f"{MODEL} via {codex_version()} (reasoning {EFFORT})", "prompt_sha256": sha256_bytes(PROMPT.read_bytes()),
                    "spec": {f: sha256_bytes((SPEC / f).read_bytes()) for f in SPEC_FILES}, "via": "single-file ODF -> ODT package (pack_fodt)",
                    "external_service": "OpenAI (Codex CLI, ChatGPT subscription)"}}
    if len(parts) == len(chunks):
        xml = parts[0] if len(parts) == 1 else merge_fodt(parts)
        x = xml.encode("utf-8")
        odt = pack_fodt(x)
        (rd / f"{LABEL}.fodt").write_bytes(x)
        (rd / f"{LABEL}.odt").write_bytes(odt)
        ok = [a for a in attempts if a.get("packed")]
        rec.update(status="ok", sha256=sha256_bytes(odt), elapsed_s=round(sum(a.get("seconds") or 0 for a in ok), 1),
                   tokens=sum(a.get("tokens") or 0 for a in ok), commands=sum(a.get("commands") or 0 for a in ok), **rng_check(x))
    else:
        rec.update(status="failed", reason=str(res.get("error", ""))[:300])
    write_json(rd / f"{LABEL}.json", rec)
    log(json.dumps({"task_id": c["task_id"][:24], "status": rec["status"], "pages": len(pg), "tokens": rec.get("tokens"),
                    "s": rec.get("elapsed_s"), "rng_valid": rec.get("rng_valid"), "commands": rec.get("commands")}, ensure_ascii=False))


# ---------------------------------------------------------------- evaluation
def evaluate() -> None:
    vis = _mod("editv", "evaluate_visual.py")
    todo = [str(task_dir(c) / "recon" / f"{LABEL}.odt") for c in sample() if (task_dir(c) / "recon" / f"{LABEL}.odt").exists()]
    todo = [f for f in todo if vis.stale(Path(f))]
    print(f"# visual: {len(todo)} to score", flush=True)
    if todo:
        vis.batch(todo)
    cost = _mod("editc", "evaluate_cost.py")
    cost.LABELS = [BASE_LABEL, LABEL]
    run_dir = Path(path("data", "runs", RUN_ID))
    run_dir.mkdir(parents=True, exist_ok=True)
    for c in sample():
        for lg in cost.task_job((str(path("data", "observed_contracts", c["task_id"].replace("~", "__") + ".json")), str(run_dir))):
            print(json.dumps(lg, ensure_ascii=False), flush=True)


def report() -> None:
    run_dir = Path(path("data", "runs", RUN_ID))
    rows = []
    for c in sample():
        tid = c["task_id"].replace("~", "__")
        r = {"task_id": tid, "format": c["format"], "language": c.get("language"), "pages": len(pages(task_dir(c)))}
        orig = json.load(open(run_dir / "original" / f"{tid}.json"))
        for lab, key in [(BASE_LABEL, "html"), (LABEL, "odt")]:
            cr = json.load(open(run_dir / lab / f"{tid}.json"))
            meta = json.load(open(task_dir(c) / "recon" / f"{lab}.json"))
            vf = task_dir(c) / "recon" / f"{lab}.visual.json"
            v = json.load(open(vf)) if vf.exists() else {}
            r[key] = {"status": meta["status"], "cost_s": cr.get("cost"), "ratio": round(cr["cost"] / orig["cost"], 3) if cr.get("cost") else None,
                      "ssim_blur": (round(v["ssim_blur"] * max(v["pages_native"], v["pages_recon"]) / max(1, v["pages_native"]), 4)
                                    if v.get("ssim_blur") is not None else None),   # paper definition (report): mean over the original's pages
                      "pages_recon": v.get("pages_recon"), "pages_native": v.get("pages_native")}
            if lab == LABEL:
                r[key].update(rng_valid=meta.get("rng_valid"), commands=meta.get("commands"), tokens=meta.get("tokens"))
        rows.append(r)

    def med(xs):
        xs = [x for x in xs if x is not None]
        return round(st.median(xs), 3) if xs else None
    summ = {"run_id": RUN_ID, "created_at": utcnow(), "tasks": len(rows), "strata": {f"{k[0]}-{k[1]}": v for k, v in STRATA.items()}}
    for key in ("html", "odt"):
        xs = [r[key] for r in rows]
        summ[key] = {"ok": sum(x["status"] == "ok" for x in xs), "ratio_median": med([x["ratio"] for x in xs]),
                     "ssim_blur_median": med([x["ssim_blur"] for x in xs if x["status"] == "ok"]),
                     "page_overflow_share": round(sum(1 for x in xs if x["status"] == "ok" and (x["pages_recon"] or 0) > (x["pages_native"] or 0))
                                                  / max(1, sum(x["status"] == "ok" for x in xs)) * 100, 1)}
    pairs = [(r["html"]["ratio"], r["odt"]["ratio"]) for r in rows if r["html"]["ratio"] and r["odt"]["ratio"]]
    summ["paired"] = {"n": len(pairs), "odt_shorter": sum(b < a for a, b in pairs), "odt_longer": sum(b > a for a, b in pairs),
                      "median_ratio_odt_over_html": med([b / a for a, b in pairs])}
    summ["odt_rng_valid"] = sum(1 for r in rows if r["odt"].get("rng_valid"))
    for fmt in sorted({r["format"] for r in rows}):
        sub = [r for r in rows if r["format"] == fmt]
        summ[f"by_format_{fmt}"] = {k: med([r[k]["ratio"] for r in sub]) for k in ("html", "odt")} | {"n": len(sub)}
    REPORT.mkdir(parents=True, exist_ok=True)
    write_json(REPORT / "summary.json", summ)
    write_json(REPORT / "rows.json", rows)
    print(json.dumps(summ, ensure_ascii=False, indent=1))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "eval", "report", "sample"])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--all", action="store_true", help="all 321 tasks (full run), not the 24-task pilot sample")
    a = ap.parse_args()
    ts = all_tasks() if a.all else sample()
    if a.cmd == "sample":
        for c in ts:
            print(c["task_id"], c["format"], c.get("language"), len(pages(task_dir(c))))
        return 0
    if a.cmd == "run":
        if a.limit:
            ts = ts[: a.limit]
        print(f"# {len(ts)} tasks, model {MODEL}, {codex_version()}", flush=True)
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
