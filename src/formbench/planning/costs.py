"""Cost schemes over the frozen operator contract. All costs are non-negative integers."""
from __future__ import annotations

from .operators import load_registry


class CostModel:
    def __init__(self, scheme: str | None = None, registry: dict | None = None):
        self.reg = registry or load_registry()
        self.scheme = scheme or self.reg["main_scheme"]
        if self.scheme not in self.reg["schemes"]:
            raise ValueError(f"unknown cost scheme {self.scheme}")
        self._c = {n: o.spec["unit_cost"][self.scheme] for n, o in self.reg["operators"].items()}

    def cost(self, op: dict) -> int:
        c = self._c[op["op"]]
        if isinstance(c, dict):
            return int(c.get("base", 0)) + int(c.get("per_char", 0)) * len(op.get("text", "") or "")
        return int(c)

    def base(self, name: str) -> int:
        c = self._c[name]
        return int(c.get("base", 0)) if isinstance(c, dict) else int(c)

    def per_char(self, name: str) -> int:
        c = self._c[name]
        return int(c.get("per_char", 0)) if isinstance(c, dict) else 0

    def plan_cost(self, ops: list[dict]) -> int:
        return sum(self.cost(o) for o in ops)

    def min_base(self, names=None) -> int:
        return min(self.base(n) for n in (names or self._c))

    @staticmethod
    def content_chars(ops: list[dict]) -> int:
        return sum(len(o.get("text", "")) for o in ops if o["op"] == "type_text")

    def breakdown(self, ops: list[dict]) -> dict:
        """Content-entry vs structural-correction split (reported separately; their sum is the plan cost)."""
        content_ops = {"focus", "focus_cell", "focus_field", "move_caret", "select_range", "type_text", "delete_selection", "toggle_checkbox"}
        content = sum(self.cost(o) for o in ops if o["op"] in content_ops)
        total = self.plan_cost(ops)
        by_op: dict[str, int] = {}
        for o in ops:
            by_op[o["op"]] = by_op.get(o["op"], 0) + 1
        return {"total": total, "content_entry": content, "structural_correction": total - content, "n_ops": len(ops),
                "content_chars": self.content_chars(ops), "op_counts": dict(sorted(by_op.items()))}
