"""Operator registry: loads the frozen operator contract and checks it against the executable backend."""
from __future__ import annotations

import functools
from dataclasses import dataclass

from ..completion.editor_backends import FORBIDDEN_OPS, EditorSession
from ..util import jhash, load_yaml, path

REQUIRED_KEYS = ["name", "version", "preconditions", "parameters", "state_transition", "scope", "atomicity", "unit_cost",
                 "allowed_side_effects", "postcondition", "executable_backend", "test_cases"]
REQUIRED_CATEGORIES = {"navigation", "selection", "input", "delete", "formatting", "paragraph_structure", "table_cell_access",
                       "table_structure", "table_merge", "object_create", "object_delete", "object_move", "object_resize",
                       "anchor_change", "check_state", "repeat_item", "special_field", "undo_recovery"}


@dataclass(frozen=True)
class OperatorSpec:
    name: str
    version: str
    category: str
    spec: dict

    def __getitem__(self, k):
        return self.spec[k]


@functools.lru_cache(maxsize=4)
def load_registry(cfg_path: str | None = None) -> dict:
    raw = load_yaml(cfg_path or path("configs", "edit_operators.yaml"))
    ops = {}
    for o in raw["operators"]:
        o = dict(o)
        o.setdefault("version", raw["version"])
        ops[o["name"]] = OperatorSpec(o["name"], o["version"], o.get("category", ""), o)
    return {"version": raw["version"], "operators": ops, "forbidden": {f["name"] for f in raw.get("forbidden", [])},
            "schemes": raw["cost_schemes"], "main_scheme": raw["main_scheme"], "hash": jhash(raw), "raw": raw}


def validate_registry(reg: dict | None = None) -> list[str]:
    reg = reg or load_registry()
    errs = []
    cats = set()
    for name, o in reg["operators"].items():
        for k in REQUIRED_KEYS:
            if k not in o.spec:
                errs.append(f"{name}: missing {k}")
        if not hasattr(EditorSession, "_op_" + name):
            errs.append(f"{name}: no executable backend implementation")
        for s in reg["schemes"]:
            c = o.spec.get("unit_cost", {}).get(s)
            base = c.get("base") if isinstance(c, dict) else c
            if not isinstance(base, int) or base < 0:
                errs.append(f"{name}: cost for scheme {s} must be a non-negative integer")
        if not o.spec.get("test_cases"):
            errs.append(f"{name}: no test cases")
        cats.add(o.category)
    for c in sorted(REQUIRED_CATEGORIES - cats):
        errs.append(f"missing operator category: {c}")
    if reg["forbidden"] != FORBIDDEN_OPS:
        errs.append("forbidden operator list differs between contract and backend")
    for m in dir(EditorSession):
        if m.startswith("_op_") and m[4:] not in reg["operators"]:
            errs.append(f"backend operator without contract entry: {m[4:]}")
    return errs
