import os
import re

import pytest

from formbench import fixtures
from formbench.completion.editor_backends import EditorSession
from formbench.contracts.extract import build_contracts
from formbench.contracts.verify import DocView
from formbench.formats.hwpx import HwpxPackage

os.environ.setdefault("FORMBENCH_SQLITE_SYNCHRONOUS", "OFF")       # throw-away databases: no fsync per statement
SEED = 20261014
ASSETS = {"signature": fixtures.signature_png()}


def view_of(data: bytes) -> DocView:
    return DocView(HwpxPackage.load(data))


def contracts_for(data: bytes, template_id: str = "fx", **kw) -> dict:
    cs, _ = build_contracts(template_id, view_of(data), "synthetic_from_native", SEED, **kw)
    return {c["scenario"]: c for c in cs}


def resolve_selectors(obj, sess: EditorSession):
    """Resolve `$name` selectors used in configs/edit_operators.yaml test cases to session node ids."""
    ir = sess.ir
    top = [p for s in ir.sections for p in s.paras]

    def one(v):
        if not isinstance(v, str) or not v.startswith("$"):
            return v
        m = re.fullmatch(r"\$cell(\d+)_(\d+)_(\d+)", v)
        if m:
            t = ir.tables()[int(m.group(1))]
            return t.cell_at(int(m.group(2)), int(m.group(3))).paras[0].nid
        m = re.fullmatch(r"\$(table|para|textbox|check|field|pic)(\d+)", v)
        kind, i = m.group(1), int(m.group(2))
        if kind == "table":
            return ir.tables()[i].nid
        if kind == "para":
            return [p for p in top if p.text.strip()][i].nid
        if kind == "textbox":
            return [s for s in ir.shapes() if s.is_textbox][i].nid
        if kind == "check":
            return [s for s in ir.shapes() if s.kind == "checkBtn"][i].nid
        if kind == "pic":
            return [s for s in ir.shapes() if s.kind == "pic"][i].nid
        return [inl.meta["nid"] for p in ir.paras() for inl in p.inlines if inl.kind == "field_begin"][i]
    if isinstance(obj, dict):
        return {k: resolve_selectors(v, sess) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve_selectors(v, sess) for v in obj]
    return one(obj)


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """Isolated project root (configs are linked, data/reports are fresh)."""
    from formbench.util import ROOT
    for d in ("configs",):
        os.symlink(ROOT / d, tmp_path / d)
    (tmp_path / "AGENTS.md").write_text("sandbox")
    monkeypatch.setenv("FORMBENCH_ROOT", str(tmp_path))
    return tmp_path
