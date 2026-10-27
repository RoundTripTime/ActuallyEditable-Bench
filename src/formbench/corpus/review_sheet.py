"""Review of observed (blank, completed) pair candidates.

* ``build()`` writes a static sheet (``reports/review/observed_pairs.html``) with the decision command under each card.
* ``serve()`` runs a local-only review server (127.0.0.1): the same cards with Verified / Rejected buttons that record the
  decision immediately through ``pairing.review_observed`` (the only path to ``observed_verified``).

Page images come from documents whose redistribution rights are often unverified, so everything stays under
``reports/review/`` (git-ignored) or on the loopback interface and is never published."""
from __future__ import annotations

import html
import json
import re
import threading
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from ..collection.discovery import blob_path
from ..formats.hwpx import HwpxPackage
from ..formats.renderers import InternalRenderer, pdf_to_pngs
from ..util import DB, path, write_text
from . import pairing
from .importers import derived_hwpx_path

THUMBS = ("reports", "review", "thumbs")
RENDER_LOCK = threading.Lock()
PAIR_TYPES = ("observed_candidate", "observed_verified", "observed_rejected", "observed_auto_rejected", "observed_excluded",
              "observed_duplicate")


def _pdf_of(doc: dict) -> bytes:
    """Preview PDF from the ORIGINAL file through LibreOffice, so text boxes, callouts and colours (where publishers put
    the guidance of an example) are visible; the internal HWPX path dropped them (review 2026-10-15). Fallback: internal."""
    if doc.get("original_format") == "pdf":
        return blob_path(doc["sha256"]).read_bytes()
    if doc.get("original_format") in ("docx", "odt", "hwpx"):
        from ..formats.renderers import LibreOfficeRenderer
        pdf, _err = LibreOfficeRenderer().convert(blob_path(doc["sha256"]).read_bytes(), "." + doc["original_format"],
                                                  timeout=120, isolated_profile=True)
        if pdf:
            return pdf
    hx = derived_hwpx_path(doc["document_id"])
    data = hx.read_bytes() if hx.exists() else blob_path(doc["sha256"]).read_bytes()
    return InternalRenderer().render(HwpxPackage.load(data)).pdf


def thumb(doc: dict, page: int = 0, dpi: int = 60) -> str | None:
    """Cached PNG of one page (relative URL under reports/review/), or None if it cannot be rendered."""
    if not doc:
        return None
    name = f"{doc['document_id']}_p{page}.lo.png"     # .lo: rendered from the original by LibreOffice (older previews are ignored)
    p = path(*THUMBS, name)
    if not p.exists():
        try:
            pngs = pdf_to_pngs(_pdf_of(doc), dpi)
            if page >= len(pngs):
                return None
            p.parent.mkdir(parents=True, exist_ok=True)
            for k, png in enumerate(pngs[:2]):          # one conversion serves both preview pages
                path(*THUMBS, f"{doc['document_id']}_p{k}.lo.png").write_bytes(png)
        except Exception:      # noqa: BLE001 - a missing preview is shown as such, the pair is still listed
            return None
    return "thumbs/" + name


GRADE_ORDER = {"strong": 0, "medium": 1, "weak": 2}
AUTO_TEXT = {"texts_differ": "less than 35 % of the blank's text appears in the example (no verified pair was ever below this)",
             "not_filled": "the example adds no typed values: same text as the blank, a stamp, or guidance sentences only"}
PER_PAGE = 25


def _cards(db: DB, interactive: bool, only: str | None = None, grade: str | None = None, page: int = 1,
           per_page: int | None = None) -> tuple[list[str], dict]:
    """Cards of one page. The interactive server never renders previews here (the browser asks /thumbs/... and the
    server renders on demand), so a page with hundreds of candidates opens at once."""
    rows = db.q(f"SELECT * FROM pairs WHERE pair_type IN ({','.join('?' * len(PAIR_TYPES))})", PAIR_TYPES)
    counts = {t: 0 for t in PAIR_TYPES}
    sel = []
    for r in rows:
        counts[r["pair_type"]] += 1
        ev = json.loads(r["evidence"] or "{}")
        if (only and r["pair_type"] != only) or (grade and ev.get("grade") != grade):
            continue
        sel.append((PAIR_TYPES.index(r["pair_type"]), GRADE_ORDER.get(ev.get("grade"), 3), -(r["confidence"] or 0), r["pair_id"], r, ev))
    sel.sort(key=lambda x: x[:4])
    counts["shown"] = len(sel)
    if per_page:
        counts["pages"] = max(1, -(-len(sel) // per_page))
        sel = sel[(page - 1) * per_page: page * per_page]
    cards = []
    for *_, r, ev in sel:
        b, e = db.get_doc(r["template_id"]) or {}, db.get_doc(r["completed_id"]) or {}

        def fig(d, role):
            label = html.escape(f"{role} · {d.get('original_format')} · {d.get('document_id')}")
            if interactive:                            # rendered on request by the server (/thumbs/<id>_p<k>.png)
                imgs = "".join(f'<img loading="lazy" src="/thumbs/{d.get("document_id")}_p{k}.lo.png" alt="{label} p{k + 1}" '
                               f'onerror="this.style.display=\'none\'">' for k in range(2)) if d.get("document_id") else ""
            else:                                      # static sheet: existing previews only (rendering is the server's / prerender's job)
                imgs = "".join(f'<img loading="lazy" src="thumbs/{d.get("document_id")}_p{k}.lo.png" alt="{label} p{k + 1}">' for k in range(2)
                               if d.get("document_id") and path(*THUMBS, f'{d.get("document_id")}_p{k}.lo.png').exists())
            link = ""
            return f"<figure>{imgs or '<div class=miss>no preview</div>'}<figcaption>{label}{link}</figcaption></figure>"
        rev = ev.get("review") or {}
        auto = ev.get("auto_review") or {}
        auto_html = (f"<p class=auto>auto-rejected ({html.escape(auto.get('rule', ''))}, rules v{html.escape(str(auto.get('version')))}): "
                     f"{html.escape(AUTO_TEXT.get(auto.get('rule'), ''))}</p>") if r["pair_type"] == "observed_auto_rejected" else ""
        dup = ev.get("duplicate") or {}
        if r["pair_type"] == "observed_duplicate":
            auto_html += (f"<p class=auto>duplicate of <a href=\"#{html.escape(dup.get('of', ''))}\">{html.escape(dup.get('of', ''))}</a> "
                          f"({html.escape(str(dup.get('rule')))}; bigram {dup.get('bigram_jaccard')}, layout distance {dup.get('phash_distance')}; "
                          f"was {html.escape(str(dup.get('was')))})</p>")
        decided = auto_html + (f"<p class=decided>{html.escape(rev.get('decision', ''))} by {html.escape(rev.get('reviewer', ''))} — "
                               f"{html.escape(rev.get('note', ''))}</p>" if rev else "")
        pid = html.escape(r["pair_id"])
        if interactive:
            action = (f'<div class=act data-pid="{pid}"><button class=o onclick="openPair(this)" title="download both files and open them side by side">'
                      f'Open pair ◧◨</button><input class=note placeholder="note (optional)">'
                      f'<button class=v onclick="decide(this,\'verified\')">Verified</button>'
                      f'<button class=r onclick="decide(this,\'rejected\')">Rejected</button><span class=msg></span></div>')
        else:
            action = f"<pre>formbench corpus review-pairs --pair-id {pid} --decision verified --reviewer NAME --note '...'</pre>"
        alts = ", ".join(f"{a['formats'][0]}→{a['formats'][1]}" for a in (ev.get("alternate_format_pairs") or []))
        cards.append(f"""<section class="card {r['pair_type']}" id="{pid}"><h2>{pid} <small>{r['pair_type']} · grade {html.escape(str(ev.get('grade')))} · confidence {r['confidence']}</small></h2>
<p><a href="{html.escape(ev.get('page', ''))}" target="_blank" rel="noreferrer">{html.escape(ev.get('page', ''))}</a><br>{f'blank listed on another page of the same site: <a href="{html.escape(ev["blank_page"])}" target="_blank" rel="noreferrer">{html.escape(ev["blank_page"])}</a><br>' if ev.get('blank_page') else ''}
blank label: {html.escape(str(ev.get('publisher_label_blank')))}<br>example label: {html.escape(str(ev.get('publisher_label_example')))}<br>
name relation {html.escape(str(ev.get('name_relation')))} · text containment {ev.get('template_text_containment')} · same template family {ev.get('same_template_family')}
· rights {html.escape(str(ev.get('rights')))}{' · other formats: ' + html.escape(alts) if alts else ''}</p>
<div class="pair">{fig(b, 'blank')}{fig(e, 'completed')}</div>{decided}{action}</section>""")
    return cards, counts


STYLE = """body{font:14px system-ui;margin:16px auto;max-width:1300px;padding:0 16px} .card{border:1px solid #ccc;border-radius:8px;padding:12px;margin:12px 0}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:12px} img{width:49%;border:1px solid #ddd;margin-right:1%} figure{margin:0}
.miss{background:#f4f4f4;padding:40px;text-align:center} .observed_verified{border-color:#2a7;background:#f3fbf6} .observed_rejected{opacity:.55} .observed_auto_rejected{opacity:.7;border-style:dashed} .observed_excluded{opacity:.4;border-style:dotted} .observed_duplicate{opacity:.5;border-style:dotted} .auto{color:#a40}
nav{margin:6px 0} pre{white-space:pre-wrap;background:#f7f7f7;padding:6px} small{color:#666;font-weight:normal} .act{display:flex;gap:8px;align-items:center;margin-top:8px}
.note{flex:1;padding:6px} button{padding:6px 14px;border-radius:6px;border:1px solid #888;cursor:pointer} .v{background:#e6f6ec} .o{background:#e8f0fe;font-weight:600} .r{background:#fbeaea}
header{position:sticky;top:0;background:#fff;padding:8px 0;border-bottom:1px solid #ddd;z-index:1} .decided{color:#264}
@media (max-width:800px){.pair{grid-template-columns:1fr}}"""

SCRIPT = """<script>
const rv=document.getElementById('reviewer');
try{rv.value=localStorage.getItem('fb_reviewer')||''}catch(e){}
rv.addEventListener('change',()=>{try{localStorage.setItem('fb_reviewer',rv.value)}catch(e){}});
async function openPair(btn){
  const box=btn.closest('.act'), msg=box.querySelector('.msg');
  msg.textContent='downloading and opening…';
  const r=await fetch('/api/open_pair',{method:'POST',headers:{'Content-Type':'application/json','X-Formbench-Review':'1'},
    body:JSON.stringify({pair_id:box.dataset.pid})});
  const j=await r.json();
  msg.textContent=r.ok?(j.status==='opening'?'opened: left = blank, right = example ('+j.folder+')':j.reason+' '+j.folder):'error: '+(j.error||r.status);
}
async function decide(btn,decision){
  const box=btn.closest('.act'), msg=box.querySelector('.msg');
  if(!rv.value.trim()){msg.textContent='enter your name at the top first';rv.focus();return}
  msg.textContent='saving…';
  const r=await fetch('/api/decide',{method:'POST',headers:{'Content-Type':'application/json','X-Formbench-Review':'1'},
    body:JSON.stringify({pair_id:box.dataset.pid,decision,reviewer:rv.value.trim(),note:box.querySelector('.note').value})});
  const j=await r.json();
  if(!r.ok){msg.textContent='error: '+(j.error||r.status);return}
  const card=box.closest('.card'); card.className='card '+j.pair_type; msg.textContent=j.pair_type+' ✓';
  document.getElementById('counts').textContent=JSON.stringify(j.counts);
}
</script>"""


def render(db: DB, interactive: bool, only: str | None = None, grade: str | None = None, page: int = 1) -> str:
    cards, counts = _cards(db, interactive, only, grade, page, PER_PAGE if interactive else None)
    total = sum(counts.get(t, 0) for t in PAIR_TYPES)
    head = nav = ""
    if interactive:
        def link(**kw):
            q = {"only": only, "grade": grade, "page": page, **kw}
            return "/?" + "&".join(f"{k}={v}" for k, v in q.items() if v)
        filt = " | ".join(f'<a href="{link(only=t, page=1)}">{n}</a>' for t, n in
                          (("observed_candidate", "open"), ("observed_verified", "verified"), ("observed_rejected", "rejected"),
                                                    ("observed_auto_rejected", "auto-rejected"), ("observed_excluded", "excluded"),
                                                    ("observed_duplicate", "duplicate")))
        grades = " | ".join(f'<a href="{link(grade=g, page=1)}">{g}</a>' for g in ("strong", "medium", "weak"))
        pages = counts.get("pages", 1)
        nav = (f'<nav>page {page} / {pages} ({counts.get("shown", 0)} pairs) · '
               + (f'<a href="{link(page=page - 1)}">‹ prev</a> ' if page > 1 else "")
               + (f'<a href="{link(page=page + 1)}">next ›</a>' if page < pages else "") + "</nav>")
        head = ('<header>Reviewer: <input id="reviewer" placeholder="your name (required)"> · counts: <span id="counts">'
                f'{html.escape(json.dumps({t: counts.get(t, 0) for t in PAIR_TYPES}))}</span> · show: <a href="/">all</a> | {filt} · '
                f'grade: {grades}<br>{nav}</header>')
    return f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Observed pair review</title><style>{STYLE}</style>
{head}<h1>Observed (blank, completed) pairs — {total}</h1>
<p>Verify only if both files are the same form (same template family and version), the second is a completion of the first, and it contains
no real personal data. Reject otherwise (different form, instructions instead of a form, revised version, real data). Not for publication:
contains documents whose redistribution rights are unverified.</p>{''.join(cards)}{nav}{SCRIPT if interactive else ''}"""


def build(db: DB | None = None) -> dict:
    db = db or DB()
    page = render(db, interactive=False)
    out = path("reports", "review", "observed_pairs.html")
    write_text(out, page)
    return {"file": str(out), "pairs": page.count('<section class="card')}


def serve(host: str = "127.0.0.1", port: int = 8765, log=print) -> None:
    """Local review server. Loopback only: decisions are written to the project database."""
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("the review server only binds to the loopback interface")

    class H(BaseHTTPRequestHandler):
        def _send(self, code, body: bytes, ctype="text/html; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            u = urlsplit(self.path)
            db = DB()
            if u.path == "/":
                qs = parse_qs(u.query)
                only = qs.get("only", [None])[0]
                grade = qs.get("grade", [None])[0]
                try:
                    page = max(1, int(qs.get("page", ["1"])[0]))
                except ValueError:
                    page = 1
                return self._send(200, render(db, True, only if only in PAIR_TYPES else None,
                                              grade if grade in GRADE_ORDER else None, page).encode())
            if u.path.startswith("/thumbs/"):
                name = u.path.rsplit("/", 1)[-1]
                p = path(*THUMBS, name)
                m = re.fullmatch(r"(doc-[0-9a-f]+)_p(\d)\.lo\.png", name)
                if not p.exists() and m:
                    with RENDER_LOCK:                  # one LibreOffice render at a time
                        if not p.exists():
                            thumb(db.get_doc(m.group(1)) or {}, int(m.group(2)))
                return self._send(200, p.read_bytes(), "image/png") if p.exists() else self._send(404, b"")
            if u.path.startswith("/file/"):
                d = db.get_doc(u.path.rsplit("/", 1)[-1])
                if not d or not blob_path(d["sha256"]).exists():
                    return self._send(404, b"")
                ctype = d.get("mime_type") or mimetypes.guess_type(f"x.{d.get('original_format')}")[0] or "application/octet-stream"
                return self._send(200, blob_path(d["sha256"]).read_bytes(), ctype)
            return self._send(404, b"not found")

        def do_POST(self):  # noqa: N802
            if self.path not in ("/api/decide", "/api/open_pair") or self.headers.get("X-Formbench-Review") != "1":
                return self._send(403, b'{"error":"forbidden"}', "application/json")
            if self.path == "/api/open_pair":
                try:
                    req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0") or 0)))
                    from .review_open import open_pair
                    return self._send(200, json.dumps(open_pair(DB(), str(req["pair_id"]), log=log)).encode(), "application/json")
                except (KeyError, ValueError, OSError) as e:
                    return self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0") or 0)))
                db = DB()
                out = pairing.review_observed(db, str(req["pair_id"]), str(req["decision"]), str(req.get("reviewer", "")), str(req.get("note", ""))[:500])
                counts = {t: db.one("SELECT count(*) AS n FROM pairs WHERE pair_type=?", (t,))["n"] for t in PAIR_TYPES}
                log(f"review {out['pair_id']} -> {out['pair_type']} by {req.get('reviewer')}")
                return self._send(200, json.dumps(dict(out, counts=counts)).encode(), "application/json")
            except (KeyError, ValueError) as e:
                return self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")

        def log_message(self, *a):
            pass

    log(f"review server on http://{host}:{port}/ (Ctrl-C to stop)")
    ThreadingHTTPServer((host, port), H).serve_forever()


def prerender(db: DB | None = None, log=print, workers: int = 4) -> dict:
    """Render the previews of open candidates in review order (strong first), so pages open without waiting."""
    db = db or DB()
    cards = []
    for r in db.q("SELECT * FROM pairs WHERE pair_type='observed_candidate'"):
        ev = json.loads(r["evidence"] or "{}")
        cards.append((GRADE_ORDER.get(ev.get("grade"), 3), -(r["confidence"] or 0), r["pair_id"], r["template_id"], r["completed_id"]))
    todo = [i for *_, b, e in sorted(cards) for i in (b, e) if not path(*THUMBS, f"{i}_p0.lo.png").exists()]
    todo = list(dict.fromkeys(todo))
    docs = {i: db.get_doc(i) or {} for i in todo}
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(workers) as ex:              # each conversion runs its own soffice with its own profile
        done = sum(1 for t in ex.map(lambda i: thumb(docs[i], 0), todo) if t)
    return {"rendered": done, "attempted": len(todo)}

