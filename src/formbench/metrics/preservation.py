"""non_target_preservation aggregated over tasks (protected text / merges / object references)."""
from __future__ import annotations


def aggregate(rows: list[dict]) -> dict:
    vals = [r["preservation"] for r in rows if r.get("preservation") is not None]
    done = [r for r in rows if r.get("replay_ok")]
    return {"tasks_replayed": len(done), "mean_pooled_preservation": round(sum(vals) / len(vals), 4) if vals else None,
            "all_protected_ok": sum(1 for r in done if r.get("protected_ok")), "protected_violation_tasks": sum(1 for r in done if not r.get("protected_ok"))}
