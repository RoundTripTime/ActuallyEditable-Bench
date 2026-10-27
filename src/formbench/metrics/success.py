"""completion_success_rate and solver_coverage with explicit numerators / denominators and Wilson intervals."""
from __future__ import annotations

import math


def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (round(max(0.0, c - h), 4), round(min(1.0, c + h), 4))


def rate(rows: list[dict], key: str = "completion_success") -> dict:
    n = len(rows)
    k = sum(1 for r in rows if r.get(key))
    lo, hi = wilson(k, n)
    return {"numerator": k, "denominator": n, "rate": round(k / n, 4) if n else None, "wilson95": [lo, hi]}


def solver_coverage(rows: list[dict]) -> dict:
    n = len(rows)
    by = {}
    for r in rows:
        by[r.get("solver_status") or r.get("status") or "unknown"] = by.get(r.get("solver_status") or r.get("status") or "unknown", 0) + 1
    exact = sum(1 for r in rows if r.get("exact_minimum"))
    return {"tasks": n, "exact_proven": exact, "exact_rate": round(exact / n, 4) if n else None, "by_solver_status": dict(sorted(by.items())),
            "timeout_rate": round(sum(1 for r in rows if r.get("status") == "timeout") / n, 4) if n else None,
            "unsupported_rate": round(sum(1 for r in rows if r.get("status") in ("unsupported", "recon_failed", "recon_blocked")) / n, 4) if n else None}
