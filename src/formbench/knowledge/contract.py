"""Consistency check between the human-action operator contract (configs/edit_operators_v2.yaml) and the editor
knowledge base. Deterministic; run by `formbench kb check-contract` and by the test suite.

Rules
* every fact an operator cites exists and is verified (or cited_not_snapshotted);
* an operator admitted as ``common`` needs every concept to have kb status ``common``;
* an operator admitted as ``conditional`` must state a ``condition`` and ``guard`` list, and none of its concepts may be
  a kb ``gap`` or ``no_shared_action`` (the condition can reconcile differing effects, not missing evidence);
* an ``unsupported`` operator must give a ``reason``;
* forbidden names never appear as operators."""
from __future__ import annotations

from ..util import jhash, load_yaml, path
from . import kb

ADMISSIONS = ("common", "conditional", "unsupported")


def load_contract() -> dict:
    return load_yaml(path("configs", "edit_operators_v2.yaml"))


def check(contract: dict | None = None) -> dict:
    c = contract or load_contract()
    ver = kb.verify()
    ok_facts = {r["id"] for r in ver["results"] if r["status"] in kb.OK_STATUSES}
    all_facts = {r["id"] for r in ver["results"]}
    concepts = {x["concept"]: x for x in kb.concept_coverage(ver)}
    errs, rows = [], []
    names = [o["name"] for o in c["operators"]]
    if len(names) != len(set(names)):
        errs.append("duplicate operator names")
    for o in c["operators"]:
        n, adm = o["name"], o.get("admission")
        if n in set(c.get("forbidden", [])):
            errs.append(f"{n}: forbidden operator listed")
        if adm not in ADMISSIONS:
            errs.append(f"{n}: admission must be one of {ADMISSIONS}")
            continue
        for f in o.get("facts", []):
            if f not in all_facts:
                errs.append(f"{n}: cites unknown fact {f}")
            elif f not in ok_facts:
                errs.append(f"{n}: cites unverified fact {f}")
        st = {k: (concepts.get(k) or {}).get("status", "gap") for k in o.get("concepts", [])}
        if adm == "common" and any(v != "common" for v in st.values()):
            errs.append(f"{n}: admitted as common but kb concept status is {st}")
        if adm == "conditional":
            if not o.get("condition") or not o.get("guard"):
                errs.append(f"{n}: conditional admission needs condition and guard")
            if any(v in ("gap", "no_shared_action") for v in st.values()):
                errs.append(f"{n}: conditional admission over missing evidence {st}")
        if adm == "unsupported" and not o.get("reason"):
            errs.append(f"{n}: unsupported needs a reason")
        if "actions" not in (o.get("unit_cost") or {}):
            errs.append(f"{n}: unit_cost.actions missing")
        rows.append({"operator": n, "admission": adm, "concepts": st, "facts": len(o.get("facts", []))})
    by = {}
    for r in rows:
        by[r["admission"]] = by.get(r["admission"], 0) + 1
    return {"version": c.get("version"), "operators": rows, "by_admission": by, "errors": errs,
            "kb_hash": ver["hash"], "contract_hash": jhash(c)}
