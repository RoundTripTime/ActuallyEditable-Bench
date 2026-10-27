"""Representation invariance (AGENTS.md §7, test #1): style-id renumbering, run splitting and attribute order must
not change the semantic fingerprint nor the minimum completion cost."""
import copy

import pytest
from lxml import etree

from conftest import contracts_for
from formbench import fixtures
from formbench.document_ir.normalize import build_ir, semantic_fingerprint
from formbench.formats.hwpx import HH, HP, HwpxPackage
from formbench.planning.search import solve


def _renumber_styles(data: bytes, shift: int = 137) -> bytes:
    pkg = HwpxPackage.load(data)
    head = pkg.header
    for tag, ref in (("charPr", "charPrIDRef"), ("paraPr", "paraPrIDRef"), ("borderFill", "borderFillIDRef")):
        ids = {e.get("id") for e in head.iter(HH + tag)}
        for e in head.iter(HH + tag):
            e.set("id", str(int(e.get("id")) + shift))
        for root in [head] + pkg.sections:
            for el in root.iter():
                if isinstance(el.tag, str) and el.get(ref) in ids:
                    el.set(ref, str(int(el.get(ref)) + shift))
    pkg.mark_dirty(pkg.header_name)
    pkg.mark_dirty()
    return pkg.to_bytes()


def _split_runs(data: bytes) -> bytes:
    pkg = HwpxPackage.load(data)
    for root in pkg.sections:
        for run in list(root.iter(HP + "run")):
            t = run.find(HP + "t")
            if t is not None and t.text and len(t.text) >= 2 and len(run) == 1 and len(t) == 0:
                new = copy.deepcopy(run)
                k = len(t.text) // 2
                new.find(HP + "t").text, t.text = t.text[k:], t.text[:k]
                run.addnext(new)
    pkg.mark_dirty()
    return pkg.to_bytes()


def _reverse_attributes(data: bytes) -> bytes:
    pkg = HwpxPackage.load(data)
    for root in [pkg.header] + pkg.sections:
        for el in root.iter():
            if isinstance(el.tag, str) and len(el.attrib) > 1:
                items = list(el.attrib.items())
                el.attrib.clear()
                for k, v in reversed(items):
                    el.set(k, v)
    pkg.mark_dirty(pkg.header_name)
    pkg.mark_dirty()
    return pkg.to_bytes()


TRANSFORMS = {"renumber_style_ids": _renumber_styles, "split_runs": _split_runs, "reverse_attribute_order": _reverse_attributes,
              "all": lambda d: _reverse_attributes(_split_runs(_renumber_styles(d)))}


@pytest.mark.parametrize("fixture", ["simple_form", "merged_form", "repeat_form", "field_form", "checkbox_form"])
@pytest.mark.parametrize("name", sorted(TRANSFORMS))
def test_equivalent_representations_have_equal_fingerprint_and_cost(fixture, name):
    data = fixtures.build(fixture)
    other = TRANSFORMS[name](data)
    assert other != data
    assert etree.tostring(HwpxPackage.load(other).sections[0]) != etree.tostring(HwpxPackage.load(data).sections[0]) or name == "renumber_style_ids"
    assert semantic_fingerprint(build_ir(HwpxPackage.load(data))) == semantic_fingerprint(build_ir(HwpxPackage.load(other)))
    for scn, c in contracts_for(data).items():
        if scn == "image":
            continue
        a, b = solve(c, data), solve(c, other)
        assert a.status == b.status == "exact"
        assert a.cost == b.cost, (fixture, name, scn)
        for scheme in ("structural", "klm_ms"):
            assert solve(c, data, scheme).cost == solve(c, other, scheme).cost


def test_fingerprint_is_sensitive_to_real_changes():
    data = fixtures.build("simple_form")
    pkg = HwpxPackage.load(data)
    t = next(e for e in pkg.sections[0].iter(HP + "t") if e.text == "성명")
    t.text = "성 명"
    pkg.mark_dirty()
    assert semantic_fingerprint(build_ir(HwpxPackage.load(pkg.to_bytes()))) != semantic_fingerprint(build_ir(HwpxPackage.load(data)))
