"""relative_editing_overhead: DeltaCost and RelativeOverhead between a reconstruction and the original template.
Negative values are kept (a reconstruction can be cheaper to fill). Only pairs where both sides have a proven exact
minimum enter the exact distribution; everything else is reported as a range from proven bounds."""
from __future__ import annotations


def delta_cost(c_recon: int, c_orig: int) -> int:
    return c_recon - c_orig


def relative_overhead(c_recon: int, c_orig: int) -> float:
    return (c_recon - c_orig) / max(c_orig, 1)


def pair_overhead(orig: dict, recon: dict) -> dict:
    """orig / recon: result rows for the same task and cost scheme."""
    out = {"comparable_exact": False, "delta_cost": None, "relative_overhead": None, "delta_range": None}
    if not orig.get("completion_success") or orig.get("cost") is None:
        out["reason"] = "original_not_completed"
        return out
    if recon.get("completion_success") and recon.get("exact_minimum") and orig.get("exact_minimum"):
        out.update({"comparable_exact": True, "delta_cost": delta_cost(recon["cost"], orig["cost"]),
                    "relative_overhead": round(relative_overhead(recon["cost"], orig["cost"]), 6)})
        return out
    if recon.get("completion_success") and recon.get("upper_bound") is not None:
        lo_o = orig["lower_bound"] if orig.get("lower_bound") is not None else 0
        hi_o = orig["upper_bound"] if orig.get("upper_bound") is not None else orig["cost"]
        lo_r = recon.get("lower_bound") or 0
        out["delta_range"] = [lo_r - hi_o, recon["upper_bound"] - lo_o]          # proven interval for DeltaCost
        out["reason"] = "bounded"
        return out
    out["reason"] = "recon_not_completed"
    return out
