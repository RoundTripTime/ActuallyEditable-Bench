"""visual_similarity: page-raster comparison under identical renderer / DPI / font conditions (SSIM, ink IoU) plus
character error rate of native text. Mismatching render conditions produce an explicit warning and are reported in
a separate cross-renderer table, never mixed with same-condition scores."""
from __future__ import annotations

import io

import numpy as np
from PIL import Image
from skimage.metrics import structural_similarity

from ..planning.dp import levenshtein_fast
from ..util import Status


def _gray(png: bytes, size: tuple[int, int] | None = None) -> np.ndarray:
    im = Image.open(io.BytesIO(png)).convert("L")
    if size and im.size != size:
        im = im.resize(size, Image.BILINEAR)
    return np.asarray(im)


def condition_key(meta: dict) -> tuple:
    return (meta.get("renderer"), meta.get("dpi"), tuple(sorted((meta.get("fonts") or {}).items())) if isinstance(meta.get("fonts"), dict) else meta.get("fonts"))


def page_similarity(a: bytes, b: bytes) -> dict:
    ga = _gray(a)
    gb = _gray(b, (ga.shape[1], ga.shape[0]))
    ssim = float(structural_similarity(ga, gb, data_range=255))
    ia, ib = ga < 160, gb < 160
    from scipy.ndimage import binary_dilation
    da, db_ = binary_dilation(ia, iterations=1), binary_dilation(ib, iterations=1)
    union = (da | db_).sum()
    return {"ssim": round(ssim, 4), "ink_iou": round(float((da & db_).sum() / union), 4) if union else 1.0,
            "ink_ratio_a": round(float(ia.mean()), 4), "ink_ratio_b": round(float(ib.mean()), 4)}


def visual_similarity(pages_a: list[bytes], pages_b: list[bytes], meta_a: dict, meta_b: dict) -> dict:
    """Mean per-page scores; a missing page scores 0. ``comparable`` is False when render conditions differ."""
    same = condition_key(meta_a) == condition_key(meta_b)
    n = max(len(pages_a), len(pages_b))
    if n == 0:
        return {"status": Status.FAILED, "reason": "no pages"}
    per = [page_similarity(pages_a[i], pages_b[i]) if i < len(pages_a) and i < len(pages_b) else {"ssim": 0.0, "ink_iou": 0.0} for i in range(n)]
    out = {"status": Status.OK, "comparable": same, "ssim": round(sum(p["ssim"] for p in per) / n, 4), "ink_iou": round(sum(p["ink_iou"] for p in per) / n, 4),
           "pages_a": len(pages_a), "pages_b": len(pages_b), "page_count_match": len(pages_a) == len(pages_b)}
    if not same:
        out["warning"] = "render_condition_mismatch: renderer/DPI/fonts differ; score is reported in the cross-renderer table only"
    return out


def text_cer(reference: str, hypothesis: str, cap: int = 6000) -> float:
    """Character error rate on whitespace-free text (edit distance / reference length), capped for long documents."""
    r = "".join(reference.split())[:cap]
    h = "".join(hypothesis.split())[:cap]
    if not r:
        return 0.0 if not h else 1.0
    return round(min(1.0, levenshtein_fast(r, h) / len(r)), 4)


def lpips_available() -> tuple[bool, str]:
    try:
        import lpips  # noqa: F401
        import torch  # noqa: F401
    except ImportError as e:
        return False, f"lpips/torch not installed ({e.name})"
    return True, ""


_LPIPS = {}


def lpips_distance(a: bytes, b: bytes) -> float | None:
    ok, _ = lpips_available()
    if not ok:
        return None
    import lpips
    import torch
    if "m" not in _LPIPS:
        _LPIPS["m"] = lpips.LPIPS(net="alex", verbose=False)
    def t(png):
        im = Image.open(io.BytesIO(png)).convert("RGB").resize((384, 544), Image.BILINEAR)
        return torch.from_numpy(np.asarray(im)).permute(2, 0, 1).float().div(127.5).sub(1.0).unsqueeze(0)
    with torch.no_grad():
        return round(float(_LPIPS["m"](t(a), t(b)).item()), 4)
