"""Image adapter: metadata, perceptual hash (DCT pHash), and ink statistics for PNG/JPEG/TIFF page images."""
from __future__ import annotations

import io

import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = 120_000_000        # decompression-bomb guard


def image_info(data: bytes) -> dict:
    with Image.open(io.BytesIO(data)) as im:
        n = getattr(im, "n_frames", 1)
        dpi = im.info.get("dpi")
        g = np.asarray(im.convert("L").resize((256, 256)))
        return {"width": im.width, "height": im.height, "mode": im.mode, "format": im.format, "page_count": n,
                "dpi": [float(dpi[0]), float(dpi[1])] if dpi else None, "ink_ratio": round(float((g < 128).mean()), 4)}


def phash(data_or_array, size: int = 32, keep: int = 8) -> str:
    """64-bit DCT perceptual hash as hex."""
    from scipy.fft import dctn
    if isinstance(data_or_array, (bytes, bytearray)):
        with Image.open(io.BytesIO(data_or_array)) as im:
            g = np.asarray(im.convert("L").resize((size, size), Image.LANCZOS), dtype=np.float64)
    else:
        g = np.asarray(Image.fromarray(data_or_array).convert("L").resize((size, size), Image.LANCZOS), dtype=np.float64)
    d = dctn(g, norm="ortho")[:keep, :keep]
    med = np.median(d.flatten()[1:])
    bits = (d > med).flatten()
    return f"{int(''.join('1' if b else '0' for b in bits), 2):016x}"


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")
