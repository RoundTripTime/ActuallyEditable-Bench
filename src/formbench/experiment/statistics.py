"""Statistics for experiment reports: distributions (not just means), cluster bootstrap by template family, rank
stability across cost schemes and strata, and correlation between visual similarity and editing overhead."""
from __future__ import annotations

import math
import random

from ..metrics.overhead import pair_overhead
from ..metrics.preservation import aggregate as preservation_aggregate
from ..metrics.success import rate, solver_coverage


def quantiles(vals: list[float]) -> dict | None:
    if not vals:
        return None
    v = sorted(vals)

    def q(p):
        k = (len(v) - 1) * p
        lo, hi = math.floor(k), math.ceil(k)
        return v[lo] + (v[hi] - v[lo]) * (k - lo)
    return {"n": len(v), "min": round(v[0], 4), "q1": round(q(0.25), 4), "median": round(q(0.5), 4), "q3": round(q(0.75), 4), "max": round(v[-1], 4),
            "mean": round(sum(v) / len(v), 4)}


def cluster_bootstrap(pairs: list[tuple[str, float]], stat: str = "median", n: int = 2000, conf: float = 0.95, seed: int = 20261014) -> list[float] | None:
    """Percentile bootstrap resampling whole clusters (template families), so repeated tasks of one form are not treated as independent."""
    clusters: dict[str, list[float]] = {}
    for c, v in pairs:
        clusters.setdefault(str(c), []).append(v)
    keys = sorted(clusters)
    if len(keys) < 2:
        return None
    rng = random.Random(seed)
    f = (lambda x: sorted(x)[len(x) // 2] if len(x) % 2 else (sorted(x)[len(x) // 2 - 1] + sorted(x)[len(x) // 2]) / 2) if stat == "median" else (lambda x: sum(x) / len(x))
    stats = []
    for _ in range(n):
        sample: list[float] = []
        for _k in keys:
            sample.extend(clusters[keys[rng.randrange(len(keys))]])
        stats.append(f(sample))
    stats.sort()
    a = (1 - conf) / 2
    return [round(stats[int(a * n)], 4), round(stats[min(n - 1, int((1 - a) * n))], 4)]


def _corr(x: list[float], y: list[float], kind: str = "spearman") -> dict | None:
    if len(x) < 5 or len(set(x)) < 2 or len(set(y)) < 2:
        return None
    from scipy import stats
    r = stats.spearmanr(x, y) if kind == "spearman" else stats.kendalltau(x, y)
    return {"n": len(x), "kind": kind, "coef": round(float(r[0]), 4), "p_value": float(f"{float(r[1]):.3g}")}


def join_overheads(rows: list[dict]) -> list[dict]:
    """Attach DeltaCost / RelativeOverhead to every reconstruction row using the original row of the same task & scheme."""
    orig = {(r["task_id"], r["scheme"]): r for r in rows if r["variant"] == "original"}
    out = []
    for r in rows:
        if r["variant"] == "original":
            continue
        o = orig.get((r["task_id"], r["scheme"]))
        if o is None:
            continue
        j = dict(r)
        j["orig_cost"], j["orig_exact"], j["orig_success"] = o.get("cost"), o.get("exact_minimum"), o.get("completion_success")
        j.update(pair_overhead(o, r))
        out.append(j)
    return out


def _group_table(joined: list[dict], key, boot: dict, min_rows: int = 1) -> list[dict]:
    groups: dict = {}
    for r in joined:
        groups.setdefault(key(r), []).append(r)
    table = []
    for k in sorted(groups, key=lambda x: tuple(str(i) for i in (x if isinstance(x, tuple) else (x,)))):
        g = groups[k]
        if len(g) < min_rows:
            continue
        ex = [r for r in g if r["comparable_exact"]]
        rel = [r["relative_overhead"] for r in ex]
        dc = [r["delta_cost"] for r in ex]
        fails: dict = {}
        for r in g:
            if not r.get("completion_success"):
                fails[r.get("failure_code", "unknown")] = fails.get(r.get("failure_code", "unknown"), 0) + 1
        ssim = [r["visual_ssim"] for r in g if r.get("visual_ssim") is not None]
        cer = [r["text_cer"] for r in g if r.get("text_cer") is not None]
        table.append({
            "group": list(k) if isinstance(k, tuple) else k,
            "tasks": len(g), "templates": len({r["template_id"] for r in g}),
            "completion": rate(g), "original_completed": sum(1 for r in g if r.get("orig_success")),
            "exact_pairs": len(ex), "exact_pair_rate": round(len(ex) / len(g), 4) if g else None,
            "bounded_pairs": sum(1 for r in g if r.get("delta_range")),
            "relative_overhead": quantiles(rel), "delta_cost": quantiles(dc),
            "share_negative": round(sum(1 for d in dc if d < 0) / len(dc), 4) if dc else None,
            "share_zero": round(sum(1 for d in dc if d == 0) / len(dc), 4) if dc else None,
            "share_positive": round(sum(1 for d in dc if d > 0) / len(dc), 4) if dc else None,
            "median_ci95": cluster_bootstrap([(r.get(boot["cluster_key"]) or r["template_id"], r["relative_overhead"]) for r in ex], "median", boot["n"], boot["conf"], boot["seed"]),
            "mean_ci95": cluster_bootstrap([(r.get(boot["cluster_key"]) or r["template_id"], r["relative_overhead"]) for r in ex], "mean", boot["n"], boot["conf"], boot["seed"]),
            "bounded_delta_range": [min(r["delta_range"][0] for r in g if r.get("delta_range")), max(r["delta_range"][1] for r in g if r.get("delta_range"))]
            if any(r.get("delta_range") for r in g) else None,
            "failure_codes": dict(sorted(fails.items())), "preservation": preservation_aggregate(g), "solver": solver_coverage(g),
            "visual_ssim_mean": round(sum(ssim) / len(ssim), 4) if ssim else None, "text_cer_mean": round(sum(cer) / len(cer), 4) if cer else None,
        })
    return table


def _ranking(table: list[dict], metric: str) -> list[str]:
    def val(t):
        if metric == "completion":
            return -(t["completion"]["rate"] or 0)
        ro = t["relative_overhead"]
        return ro["median"] if ro else float("inf")
    return ["/".join(map(str, t["group"])) if isinstance(t["group"], list) else str(t["group"]) for t in sorted(table, key=lambda t: (val(t), str(t["group"])))]


def _tau(a: list[str], b: list[str]) -> float | None:
    common = [x for x in a if x in b]
    if len(common) < 3:
        return None
    from scipy import stats
    r = stats.kendalltau([a.index(x) for x in common], [b.index(x) for x in common])
    return round(float(r[0]), 4) if not math.isnan(float(r[0])) else None


def summarize(meta: dict, rows: list[dict]) -> dict:
    cfg = meta.get("config", {})
    st = cfg.get("statistics", {})
    boot = {"n": int(st.get("bootstrap_resamples", 2000)), "conf": float(st.get("confidence", 0.95)), "seed": int(meta.get("seed", 0)),
            "cluster_key": st.get("cluster_key", "template_family_id")}
    schemes = sorted({r["scheme"] for r in rows})
    main = (cfg.get("solver", {}).get("schemes") or schemes)[0]
    joined = join_overheads(rows)
    jm = [r for r in joined if r["scheme"] == main]
    orig_main = [r for r in rows if r["variant"] == "original" and r["scheme"] == main]
    out: dict = {"run_id": meta.get("run_id"), "protocol_hash": meta.get("protocol_hash"), "main_scheme": main, "schemes": schemes,
                 "denominators": {"templates": len({r["template_id"] for r in rows}), "tasks": len({r["task_id"] for r in rows}), "rows": len(rows),
                                  "rows_main_scheme": sum(1 for r in rows if r["scheme"] == main),
                                  "template_families": len({r.get("template_family_id") for r in rows}),
                                  "by_pair_type": {k: len({r["task_id"] for r in rows if r.get("pair_type") == k}) for k in sorted({str(r.get("pair_type")) for r in rows})},
                                  "by_scenario": {k: len({r["task_id"] for r in rows if r.get("scenario") == k}) for k in sorted({str(r.get("scenario")) for r in rows})}}}
    out["original"] = {"completion": rate(orig_main), "solver": solver_coverage(orig_main), "cost": quantiles([r["cost"] for r in orig_main if r.get("cost") is not None]),
                       "preservation": preservation_aggregate(orig_main),
                       "cost_by_scenario": {s: quantiles([r["cost"] for r in orig_main if r.get("scenario") == s and r.get("cost") is not None])
                                            for s in sorted({str(r.get("scenario")) for r in orig_main})}}
    out["by_baseline_input"] = _group_table(jm, lambda r: (r["baseline"], r["input"]), boot)
    out["by_baseline"] = _group_table(jm, lambda r: r["baseline"], boot)
    out["by_family"] = _group_table(jm, lambda r: r["family"], boot)
    out["by_input"] = _group_table(jm, lambda r: r["input"], boot)
    out["stratified"] = {name: _group_table(jm, (lambda r, f=f: (f(r), r["baseline"])), boot, min_rows=3) for name, f in {
        "scenario": lambda r: r.get("scenario"), "domain": lambda r: r.get("domain"), "difficulty": lambda r: r.get("difficulty"),
        "split": lambda r: r.get("split"), "pair_type": lambda r: r.get("pair_type"), "source_domain": lambda r: r.get("source_domain"),
        "pages": lambda r: "1" if (r.get("pages") or 1) <= 1 else ("2" if r.get("pages") == 2 else "3+"),
        "n_fields": lambda r: "<=5" if (r.get("n_fields") or 0) <= 5 else ("6-20" if (r.get("n_fields") or 0) <= 20 else ">20"),
        "language": lambda r: r.get("language")}.items()}
    # --- RQ2: visual similarity vs editing overhead / success
    ex = [r for r in jm if r["comparable_exact"] and r.get("visual_ssim") is not None]
    vis = [r for r in jm if r.get("visual_ssim") is not None]
    hi = [r for r in vis if r["visual_ssim"] >= 0.9]
    out["visual_vs_editability"] = {
        "spearman_ssim_vs_relative_overhead": _corr([r["visual_ssim"] for r in ex], [r["relative_overhead"] for r in ex]),
        "spearman_ssim_vs_completion": _corr([r["visual_ssim"] for r in vis], [1.0 if r.get("completion_success") else 0.0 for r in vis]),
        "high_ssim_rows": len(hi), "high_ssim_failed": sum(1 for r in hi if not r.get("completion_success")),
        "high_ssim_overhead_ge_50pct": sum(1 for r in hi if r["comparable_exact"] and r["relative_overhead"] >= 0.5),
        "high_ssim_overhead_le_0": sum(1 for r in hi if r["comparable_exact"] and r["relative_overhead"] <= 0),
        "high_ssim_relative_overhead": quantiles([r["relative_overhead"] for r in hi if r["comparable_exact"]]),
        "examples_high_ssim_high_overhead": [{"task_id": r["task_id"], "variant": r["variant"], "ssim": r["visual_ssim"], "relative_overhead": r["relative_overhead"],
                                              "delta_cost": r["delta_cost"]} for r in sorted((r for r in hi if r["comparable_exact"]), key=lambda r: -r["relative_overhead"])[:8]],
        "examples_negative_delta": [{"task_id": r["task_id"], "variant": r["variant"], "delta_cost": r["delta_cost"], "ssim": r.get("visual_ssim")}
                                    for r in sorted((r for r in jm if r["comparable_exact"] and r["delta_cost"] < 0), key=lambda r: r["delta_cost"])[:8]]}
    # --- cost-function sensitivity: do baseline rankings survive other cost schemes?
    sens = {}
    for s in schemes:
        js = [r for r in joined if r["scheme"] == s and r.get("input") in set(cfg.get("solver", {}).get("secondary_scheme_inputs", [])) | ({None} if s == main else set())]
        if s == main:
            js = [r for r in jm if r.get("input") in set(cfg.get("solver", {}).get("secondary_scheme_inputs", []))]
        tb = _group_table(js, lambda r: (r["baseline"], r["input"]), boot)
        sens[s] = {"ranking_by_median_overhead": _ranking(tb, "overhead"), "ranking_by_completion": _ranking(tb, "completion"),
                   "table": [{"group": t["group"], "completion_rate": t["completion"]["rate"], "median_rel_overhead": (t["relative_overhead"] or {}).get("median"),
                              "exact_pairs": t["exact_pairs"]} for t in tb]}
    out["cost_scheme_sensitivity"] = {"schemes": sens, "kendall_tau_vs_main": {s: {"overhead": _tau(sens[main]["ranking_by_median_overhead"], sens[s]["ranking_by_median_overhead"]),
                                                                                   "completion": _tau(sens[main]["ranking_by_completion"], sens[s]["ranking_by_completion"])}
                                                                               for s in schemes if s != main}}
    # --- naive algorithm comparators
    comp = [r for r in rows if r.get("comparators") and r["scheme"] == main]
    cdict = {}
    for name in ("string_diff", "tree_edit_distance", "greedy_heuristic"):
        xs = [(r["comparators"][name], r["comparators"]["optimal_plan"]) for r in comp if r["comparators"].get(name) is not None]
        cdict[name] = {"n": len(xs), "spearman_vs_optimal": _corr([a for a, _ in xs], [b for _, b in xs]),
                       "ratio_to_optimal": quantiles([a / b for a, b in xs if b]), "equal_to_optimal": sum(1 for a, b in xs if a == b)}
    # does a naive metric even rank reconstructions like the optimal plan does?  (delta vs original, per task)
    oc = {r["task_id"]: r["comparators"] for r in comp if r["variant"] == "original"}
    for name in ("string_diff", "tree_edit_distance", "greedy_heuristic"):
        d_naive, d_opt = [], []
        for r in comp:
            if r["variant"] != "original" and r["task_id"] in oc and r["comparators"].get(name) is not None and oc[r["task_id"]].get(name) is not None:
                d_naive.append(r["comparators"][name] - oc[r["task_id"]][name])
                d_opt.append(r["comparators"]["optimal_plan"] - oc[r["task_id"]]["optimal_plan"])
        cdict[name]["delta_spearman_vs_optimal_delta"] = _corr(d_naive, d_opt)
        cdict[name]["delta_sign_agreement"] = round(sum(1 for a, b in zip(d_naive, d_opt) if (a > 0) == (b > 0) and (a < 0) == (b < 0)) / len(d_naive), 4) if d_naive else None
    out["algorithm_comparators"] = cdict
    # --- generalisation: leave-one-stratum-out ranking stability
    gen = {}
    for name, f in {"domain": lambda r: r.get("domain"), "institution(source_domain)": lambda r: r.get("source_domain"),
                    "template_family": lambda r: r.get("template_family_id"), "scenario(long-input holdout)": lambda r: r.get("scenario")}.items():
        vals = sorted({str(f(r)) for r in jm})
        res = []
        for v in vals:
            inside = [r for r in jm if str(f(r)) == v]
            rest = [r for r in jm if str(f(r)) != v]
            if len({r["task_id"] for r in inside}) < 3 or not rest:
                continue
            ti, tr = _group_table(inside, lambda r: r["baseline"], boot), _group_table(rest, lambda r: r["baseline"], boot)
            res.append({"held_out": v, "tasks": len({r["task_id"] for r in inside}),
                        "tau_completion_ranking": _tau(_ranking(ti, "completion"), _ranking(tr, "completion")),
                        "tau_overhead_ranking": _tau(_ranking(ti, "overhead"), _ranking(tr, "overhead"))})
        taus = [x["tau_completion_ranking"] for x in res if x["tau_completion_ranking"] is not None]
        gen[name] = {"n_holdouts": len(res), "median_tau_completion": quantiles(taus)["median"] if taus else None, "holdouts": res[:40]}
    out["generalization_holdouts"] = gen
    # --- ceiling / floor
    cells = out["stratified"]["scenario"]
    out["ceiling_floor"] = {"cells": len(cells), "floor_cells(rate=0)": sum(1 for c in cells if c["completion"]["rate"] == 0),
                            "ceiling_cells(rate=1)": sum(1 for c in cells if c["completion"]["rate"] == 1),
                            "share_delta_zero_overall": round(sum(1 for r in jm if r["comparable_exact"] and r["delta_cost"] == 0) / max(1, sum(1 for r in jm if r["comparable_exact"])), 4)}
    out["error_types"] = {}
    for r in rows:
        if r["scheme"] == main and not r.get("completion_success"):
            out["error_types"][r.get("failure_code", "unknown")] = out["error_types"].get(r.get("failure_code", "unknown"), 0) + 1
    out["error_types"] = dict(sorted(out["error_types"].items(), key=lambda kv: -kv[1]))
    out["blocked"] = meta.get("blocked", [])
    out["human_validation"] = {"status": "blocked_by_environment", "note": "no human measurements were collected; no human correlation is reported"}
    return out


def human_correlation(records: list[dict]) -> dict:
    """Correlation between automatic cost and measured human effort. ``records``: {task_id, variant, cost, human_clicks, human_seconds, success}.
    Only meaningful with real measurements (v1 protocol, archived 2026-10-18); never fed with synthetic numbers in reports."""
    ok = [r for r in records if r.get("success")]
    return {"n": len(records), "n_success": len(ok),
            "spearman_cost_vs_clicks": _corr([r["cost"] for r in ok], [r["human_clicks"] for r in ok]),
            "spearman_cost_vs_seconds": _corr([r["cost"] for r in ok], [r["human_seconds"] for r in ok])}
