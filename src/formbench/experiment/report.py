"""Experiment edit report: metric ① visual similarity of the ODT reconstruction, metric ② human actions to turn it into
the example relative to doing so from the original (always finite; a failed reconstruction counts as writing the
example from an empty document). All 321 tasks, no split.
Per label: reconstruction success, visual (median SSIM after blur, CER, page match), cost ratio R/T per task (median,
geometric mean, share with R <= T), ratio of sums with a cluster bootstrap 95% interval over source domains, the share
of example blocks written from scratch, and strata (format, language). Spearman correlation of visual similarity with
the per-task ratio. Usage: python -m formbench.experiment.report <run_id> [docx,odt]"""
from __future__ import annotations

import csv
import glob
import json
import math
import random
import statistics
import sys
from pathlib import Path

from ..util import obs_name, path, utcnow, write_json

SEED = 20261018
B = 2000
# final comparison set (user decision 2026-10-18): positive control, converters on the digital PDF, parsing, small / large
# VLM, page-image control. Image-input conditions and PP-Structure were dropped from the paper set (records kept on disk).
FAMILY = {"oracle__template_odt": ("양성 대조군", "원본 → ODT 변환"), "vector_pdf__word_pdf": ("문서 변환기", "Microsoft Word · 디지털 PDF"),
          "vector_pdf__hwp_pdf": ("문서 변환기", "한컴 한/글 · 디지털 PDF"), "vector_pdf__docling_odt": ("문서 파싱", "Docling · 디지털 PDF"),
          "vector_pdf__mineru": ("소형 VLM", "MinerU2.5"), "png_150__gpt6luna_odt": ("대형 VLM", "GPT-6-Luna"),
          "png_150__image_only": ("음성 대조군", "쪽 이미지")}

CONTROLS = ("png_150__image_only", "oracle__template_odt")


def _boot(groups: dict, fn) -> tuple:
    keys = sorted(groups)
    rnd = random.Random(SEED)
    vals = []
    for _ in range(B):
        items = [it for k in (rnd.choice(keys) for _ in keys) for it in groups[k]]
        v = fn(items)
        if v is not None:
            vals.append(v)
    if not vals:
        return (None, None)
    vals.sort()
    return (round(vals[int(0.025 * len(vals))], 3), round(vals[int(0.975 * len(vals)) - 1], 3))


def build(run_id: str, formats: tuple[str, ...] | None = None) -> dict:
    """formats: restrict to these task formats (e.g. ("docx", "odt"), where the positive control validates the ceiling);
    the report is then written to reports/experiments/<run_id>--<formats>/."""
    run = Path(path("data", "runs", run_id))
    orig = {}
    for f in glob.glob(str(run / "original" / "*.json")):
        r = json.load(open(f))
        if r.get("status") == "ok" and (not formats or r.get("format") in formats):
            orig[r["task_id"]] = r
    # positive control (original converted to ODT): the same-pipeline reference for a secondary ratio R / oracle that
    # cancels the native-vs-ODT reference asymmetry
    oracle = {}
    for f in glob.glob(str(run / "oracle__template_odt" / "*.json")):
        r = json.load(open(f))
        if r.get("status") == "ok" and not r.get("from_empty_document"):
            oracle[r["task_id"]] = r["cost"]
    rows = []
    for lab in FAMILY:
        for f in glob.glob(str(run / lab / "*.json")):
            r = json.load(open(f))
            if r.get("status") != "ok" or r["task_id"] not in orig:
                continue
            t = orig[r["task_id"]]
            tid = r["task_id"].replace("~", "__")
            vis_f = Path(path("data", obs_name(), tid, "recon", f"{lab}.visual.json"))
            vis = json.load(open(vis_f)) if vis_f.exists() else {}
            body = r["zones"]["body"]
            authored = body["counts"].get("author", 0)
            rows.append({"task_id": r["task_id"], "label": lab, "family": FAMILY[lab][0], "name": FAMILY[lab][1], "format": r["format"],
                         "language": r.get("language"), "domain": r.get("source_domain"), "cost_R": r["cost"], "cost_T": t["cost"],
                         "ratio": r["cost"] / max(1, t["cost"]), "from_empty": bool(r.get("from_empty_document")),
                         "ratio_vs_oracle": r["cost"] / max(1, oracle[r["task_id"]]) if r["task_id"] in oracle else None,
                         "authored_share": authored / max(1, body["blocks_example"]),
                         "ssim_blur": vis.get("ssim_blur") if vis.get("status") == "ok" else None,
                         # mean over the ORIGINAL's pages (extra pages of the reconstruction scored 0 in ssim_blur's mean over
                         # max(pages)); page overflow is reported separately
                         "ssim_blur_native_pages": (vis["ssim_blur"] * max(vis["pages_native"], vis["pages_recon"]) / max(1, vis["pages_native"]))
                         if vis.get("status") == "ok" else None,
                         "extra_pages": (vis["pages_recon"] > vis["pages_native"]) if vis.get("status") == "ok" else None,
                         "extra_cost": r["cost"] - t["cost"],
                         "text_cer": vis.get("text_cer") if vis.get("status") == "ok" else None})
    unit = next(iter(orig.values())).get("unit", "actions") if orig else "actions"
    summary = {"run_id": run_id + (f"--{'_'.join(formats)}" if formats else ""), "formats": list(formats) if formats else "all", "created_at": utcnow(), "tasks_with_original": len(orig), "unit": unit,
               "original_cost_median": statistics.median(t["cost"] for t in orig.values()) if orig else None, "labels": {}}
    for lab in FAMILY:
        items = [x for x in rows if x["label"] == lab]
        if not items:
            continue
        rat = [x["ratio"] for x in items]
        groups = {}
        for x in items:
            groups.setdefault(x["domain"], []).append(x)
        vis = [x["ssim_blur"] for x in items if x["ssim_blur"] is not None]
        s = {"family": FAMILY[lab][0], "name": FAMILY[lab][1], "tasks": len(items),
             "reconstruction_ok": round(1 - sum(x["from_empty"] for x in items) / len(items), 4),
             "ratio_median": round(statistics.median(rat), 3), "ratio_geomean": round(math.exp(statistics.mean(math.log(max(r, 1e-9)) for r in rat)), 3),
             "ratio_of_sums": round(sum(x["cost_R"] for x in items) / max(1, sum(x["cost_T"] for x in items)), 3),
             "ratio_of_sums_ci95": _boot(groups, lambda xs: sum(x["cost_R"] for x in xs) / max(1, sum(x["cost_T"] for x in xs))),
             "share_R_le_T": round(sum(r <= 1 for r in rat) / len(rat), 4),
             "ratio_vs_oracle_median": round(statistics.median(ro), 3) if (ro := [x["ratio_vs_oracle"] for x in items if x["ratio_vs_oracle"] is not None]) else None, "cost_R_median": statistics.median(x["cost_R"] for x in items),
             "authored_share_median": round(statistics.median(x["authored_share"] for x in items), 3),
             "ssim_blur_median": round(statistics.median(vis), 4) if vis else None,
             "ssim_blur_native_pages_median": round(statistics.median(x["ssim_blur_native_pages"] for x in items if x["ssim_blur_native_pages"] is not None), 4) if vis else None,
             "extra_pages_share": round(sum(bool(x["extra_pages"]) for x in items if x["extra_pages"] is not None) / max(1, len(vis)), 4),
             "extra_cost_median": round(statistics.median(x["extra_cost"] for x in items), 2),
             "text_cer_median": round(statistics.median(x["text_cer"] for x in items if x["text_cer"] is not None), 4) if vis else None,
             "by_format": {}, "by_language": {}}
        for key, out in (("format", "by_format"), ("language", "by_language")):
            for v in sorted({x[key] for x in items if x[key]}):
                xs = [x for x in items if x[key] == v]
                s[out][v] = {"tasks": len(xs), "ratio_median": round(statistics.median(x["ratio"] for x in xs), 3)}
        summary["labels"][lab] = s
    # the controls are excluded: the page image looks like the original by construction (SSIM ~0.99) but is not editable,
    # and the positive control is the original itself; the correlation, which asks whether visual similarity predicts the editing cost among real reconstructions
    pairs = [(x["ssim_blur_native_pages"], x["ratio"]) for x in rows if x["ssim_blur"] is not None and x["label"] not in CONTROLS]
    if len(pairs) > 3:
        from scipy.stats import spearmanr
        summary["spearman_ssim_blur_vs_ratio"] = round(float(spearmanr([p[0] for p in pairs], [p[1] for p in pairs]).statistic), 3)
        hi = [p for p in pairs if p[0] >= 0.9]
        summary["ssim_ge_0_9"] = {"n": len(hi), "ratio_median": round(statistics.median(p[1] for p in hi), 3) if hi else None}
    # paired comparison with the control: share of tasks where the method needs fewer actions than writing the example
    # from the page-image document (cluster bootstrap over source domains)
    ctrl = {x["task_id"]: x["cost_R"] for x in rows if x["label"] == "png_150__image_only"}
    for lab, s in summary["labels"].items():
        if lab == "png_150__image_only":
            continue
        items = [x for x in rows if x["label"] == lab and x["task_id"] in ctrl]
        if not items:
            continue
        groups = {}
        for x in items:
            groups.setdefault(x["domain"], []).append(x)
        # a "win" over the control needs at least 10 % fewer actions (tiny differences such as deleting one more image
        # are not a better reconstruction)
        s["beats_control"] = round(sum(x["cost_R"] < 0.9 * ctrl[x["task_id"]] for x in items) / len(items), 4)
        s["beats_control_ci95"] = _boot(groups, lambda xs: sum(x["cost_R"] < 0.9 * ctrl[x["task_id"]] for x in xs) / len(xs) if xs else None)
        # saturation: share of tasks whose cost is within +-10 % of the control (the metric cannot tell the method from
        # rewriting everything)
        s["near_control"] = round(sum(abs(x["cost_R"] - ctrl[x["task_id"]]) <= 0.1 * max(1, ctrl[x["task_id"]]) for x in items) / len(items), 4)
    # separation of the methods: Friedman test over the tasks every label has (per-task ratios as repeated measures)
    # (the methods only: the two controls would inflate the separation)
    methods = [lab for lab in summary["labels"] if lab not in CONTROLS]
    common = set.intersection(*({x["task_id"] for x in rows if x["label"] == lab} for lab in methods)) if methods else set()
    if len(common) > 10 and len(methods) > 2:
        from scipy.stats import friedmanchisquare
        by = {(x["label"], x["task_id"]): x["ratio"] for x in rows}
        labs = sorted(methods)
        st = friedmanchisquare(*[[by[(lab, t)] for t in sorted(common)] for lab in labs])
        n, k = len(common), len(labs)
        summary["friedman"] = {"tasks": n, "chi2": round(float(st.statistic), 1), "p": float(st.pvalue),
                               "kendall_w": round(float(st.statistic) / (n * (k - 1)), 3)}
    summary["gap_position"] = gap_position(rows)
    summary["gap_difference_vs_docx"] = gap_difference(rows)
    out = Path(path("reports", "experiments", summary["run_id"]))
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "rows.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["task_id"])
        w.writeheader()
        w.writerows(rows)
    write_json(out / "summary.json", summary)
    (out / "report.md").write_text(markdown(summary), encoding="utf-8")
    return summary


def _gap(items: list[dict]) -> dict | None:
    """Position of each method between the ceiling (positive control, 0) and the floor (page-image control, 1), from the
    median ratios of the given tasks. None when the floor is not above the ceiling."""
    by = {}
    for x in items:
        by.setdefault(x["label"], []).append(x["ratio"])
    if "oracle__template_odt" not in by or "png_150__image_only" not in by:
        return None
    lo, hi = statistics.median(by["oracle__template_odt"]), statistics.median(by["png_150__image_only"])
    if hi - lo <= 1e-9:
        return None
    return {lab: (statistics.median(v) - lo) / (hi - lo) for lab, v in by.items() if lab not in CONTROLS}


def gap_position(rows: list[dict]) -> dict:
    """Cross-format comparison that cancels the scale of each stratum (language, document length): the methods' position
    between the positive (0) and the negative control (1), overall and per format, with a 95 % interval from a cluster
    bootstrap over source domains within the stratum (the three medians are recomputed on every resample)."""
    out = {}
    strata = {"all": rows} | {f: [x for x in rows if x["format"] == f] for f in sorted({x["format"] for x in rows})}
    for name, items in strata.items():
        point = _gap(items)
        if not point:
            continue
        groups = {}
        for x in items:
            groups.setdefault(x["domain"], []).append(x)
        keys = sorted(groups)
        rnd = random.Random(SEED)
        draws = {lab: [] for lab in point}
        for _ in range(B):
            g = _gap([it for k in (rnd.choice(keys) for _ in keys) for it in groups[k]])
            if g:
                for lab in point:
                    if lab in g:
                        draws[lab].append(g[lab])
        res = {}
        for lab, v in point.items():
            d = sorted(draws[lab])
            ci = (round(d[int(0.025 * len(d))], 3), round(d[int(0.975 * len(d)) - 1], 3)) if d else (None, None)
            res[lab] = {"position": round(v, 3), "ci95": ci}
        out[name] = {"tasks": len({x["task_id"] for x in items}), "domains": len(keys), "methods": res}
    return out


def gap_difference(rows: list[dict], base: str = "docx") -> dict:
    """Difference of the gap position between each other format and `base`, per method: independent cluster bootstraps
    over source domains within each format (B draws each), 95 % percentile interval and P(difference > 0)."""
    def draws(fmt: str, seed: int) -> list:
        g = {}
        for x in rows:
            if x["format"] == fmt:
                g.setdefault(x["domain"], []).append(x)
        keys = sorted(g)
        rnd = random.Random(seed)
        return [_gap([it for k in (rnd.choice(keys) for _ in keys) for it in g[k]]) for _ in range(B)]
    fmts = sorted({x["format"] for x in rows})
    if base not in fmts:
        return {}
    ref = draws(base, SEED)
    out = {}
    for i, fmt in enumerate(f for f in fmts if f != base):
        other = draws(fmt, SEED + 1 + i)
        res = {}
        for lab in sorted({lab for g in ref if g for lab in g}):
            d = sorted(x[lab] - y[lab] for x, y in zip(other, ref) if x and y and lab in x and lab in y)
            if d:
                res[lab] = {"median": round(d[len(d) // 2], 3), "ci95": (round(d[int(0.025 * len(d))], 3), round(d[int(0.975 * len(d)) - 1], 3)),
                            "p_gt_0": round(sum(v > 0 for v in d) / len(d), 3)}
        out[f"{fmt}-{base}"] = res
    return out


def markdown(s: dict) -> str:
    L = [f"# 실험 edit — {s['run_id']} (과제 {s['tasks_with_original']}개, 분할 없음)", "",
         (f"원본 → 예시 KLM 시간 중앙값: {s['original_cost_median']:.0f}초({s['original_cost_median'] / 60:.1f}분). 배율 = 복원본 → 예시 시간 ÷ 원본 → 예시 시간 (과제별)."
          if s.get("unit") == "seconds" else
          f"원본 → 예시 조작 수 중앙값: {s['original_cost_median']}. 배율 = 복원본 → 예시 조작 수 ÷ 원본 → 예시 조작 수 (과제별)."), "",
         f"단위: {'KLM 초' if s.get('unit') == 'seconds' else '조작 수'}. 대조군보다 적은 과제 = 음성 대조군(쪽 이미지, 전부 새로 작성)보다 10% 이상 적게 드는 과제. 양성 대조군 대비 배율 = 복원본 비용 ÷ 원본을 ODT로 바꾼 문서의 비용(같은 ODT 경로끼리 비교).", "",
         "| 계열 | 방식 | 복원 성공 | 시각 유사도 (원본 쪽 기준 블러 SSIM) | 쪽 넘침 | 배율 중앙값 | 양성 대조군 대비 배율 | 합계 배율 (95% 구간) | 추가 비용 중앙값 | 음성 대조군보다 적은 과제 (95% 구간) | 음성 대조군 ±10% 안 | 새로 작성한 블록 비율 |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|"]

    def f(v, pct=False):
        return "–" if v is None else (f"{100 * v:.1f}%" if pct else f"{v:.2f}")
    for _lab, x in sorted(s["labels"].items(), key=lambda kv: kv[1]["ratio_median"]):
        ci = x["ratio_of_sums_ci95"]
        L.append(f"| {x['family']} | {x['name']} | {f(x['reconstruction_ok'], True)} | {f(x.get('ssim_blur_native_pages_median'))} | "
                 f"{f(x.get('extra_pages_share'), True)} | {f(x['ratio_median'])} | {f(x.get('ratio_vs_oracle_median'))} | "
                 f"{f(x['ratio_of_sums'])} ({f(ci[0])}–{f(ci[1])}) | {f(x.get('extra_cost_median'))} | "
                 f"{f(x.get('beats_control'), True)} ({f((x.get('beats_control_ci95') or (None, None))[0], True)}–{f((x.get('beats_control_ci95') or (None, None))[1], True)}) | "
                 f"{f(x.get('near_control'), True)} | {f(x['authored_share_median'], True)} |")
    if "spearman_ssim_blur_vs_ratio" in s:
        L += ["", f"시각 유사도(원본 쪽 기준 블러 SSIM)와 배율의 순위 상관(대조군 제외): {s['spearman_ssim_blur_vs_ratio']}; "
                  f"SSIM ≥ 0.9인 복원 {s['ssim_ge_0_9']['n']}건의 배율 중앙값 {s['ssim_ge_0_9']['ratio_median']}"]
    gp = s.get("gap_position") or {}
    if gp:
        cols = [k for k in ("all", "docx", "odt", "hwpx") if k in gp]
        head = {"all": "전체", "docx": "DOCX", "odt": "ODT", "hwpx": "HWPX"}
        L += ["", "천장–바닥 상대 위치 (양성 대조군 = 0%, 쪽 이미지 = 100%; 배율 중앙값 기준, 95% 구간은 층 안의 도메인 부트스트랩):", "",
              "| 방식 | " + " | ".join(f"{head[c]} ({gp[c]['tasks']}과제, 도메인 {gp[c]['domains']})" for c in cols) + " |",
              "|---|" + "---|" * len(cols)]
        labs = sorted({lab for c in cols for lab in gp[c]["methods"]}, key=lambda lab: gp["all"]["methods"].get(lab, {}).get("position", 9))
        for lab in labs:
            cells = []
            for c in cols:
                m = gp[c]["methods"].get(lab)
                cells.append("–" if not m else f"{100 * m['position']:.0f}% ({f(m['ci95'][0], True)}–{f(m['ci95'][1], True)})")
            L.append(f"| {FAMILY[lab][1]} | " + " | ".join(cells) + " |")
    gd = s.get("gap_difference_vs_docx") or {}
    if gd:
        L += ["", "형식 간 차이 (상대 위치, DOCX 대비 %p; 형식마다 독립 도메인 부트스트랩, 95% 구간과 P(차이 > 0)):", "",
              "| 방식 | " + " | ".join(k.upper().replace("-", " − ") for k in gd) + " |", "|---|" + "---|" * len(gd)]
        for lab in sorted({lab for v in gd.values() for lab in v}, key=lambda lab: (gp.get("all", {}).get("methods", {}).get(lab, {}).get("position", 9))):
            L.append(f"| {FAMILY[lab][1]} | " + " | ".join(
                (lambda m: "–" if not m else f"{100 * m['median']:+.0f} ({100 * m['ci95'][0]:+.0f}, {100 * m['ci95'][1]:+.0f}); P {m['p_gt_0']:.2f}")(v.get(lab))
                for v in gd.values()) + " |")
    if "friedman" in s:
        fr = s["friedman"]
        L += ["", f"방식 간 분리(대조군 제외, 공통 과제 {fr['tasks']}개, 과제별 배율의 Friedman 검정): χ² {fr['chi2']}, p {fr['p']:.2g}, Kendall W {fr['kendall_w']}"]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    fm = tuple(sys.argv[2].split(",")) if len(sys.argv) > 2 else None
    print(json.dumps({k: v for k, v in build(sys.argv[1], fm).items() if k != "labels"}, ensure_ascii=False, indent=1))


def sensitivity(main_id: str, other_ids: list[str]) -> dict:
    """Ranking stability of the methods (by median ratio) across runs with other cost settings (e.g. tolerance)."""
    from scipy.stats import kendalltau
    base = json.load(open(path("reports", "experiments", main_id, "summary.json")))["labels"]
    out = {"main": main_id, "runs": {}}
    for oid in other_ids:
        o = json.load(open(path("reports", "experiments", oid, "summary.json")))["labels"]
        labs = sorted(set(base) & set(o))
        tau = kendalltau([base[x]["ratio_median"] for x in labs], [o[x]["ratio_median"] for x in labs]).statistic
        out["runs"][oid] = {"kendall_tau_vs_main": round(float(tau), 3), "ratio_median": {x: o[x]["ratio_median"] for x in labs}}
    write_json(path("reports", "experiments", main_id, "sensitivity.json"), out)
    return out
