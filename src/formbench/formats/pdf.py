"""PDF adapter (PyMuPDF): metadata, text, born-digital vs scanned detection, AcroForm field listing, rasterisation."""
from __future__ import annotations

import pymupdf


def pdf_info(data: bytes) -> dict:
    with pymupdf.open(stream=data, filetype="pdf") as d:
        if d.needs_pass:
            return {"page_count": d.page_count, "encrypted": True, "text": "", "is_scanned": None, "form_fields": 0}
        texts, img_area, page_area, draws, widgets = [], 0.0, 0.0, 0, []
        for page in d:
            texts.append(page.get_text())
            page_area += abs(page.rect)
            for img in page.get_image_info():
                r = pymupdf.Rect(img["bbox"])
                img_area += abs(r & page.rect)
            draws += len(page.get_drawings())
            for w in page.widgets() or []:
                widgets.append({"name": w.field_name, "type": w.field_type_string, "page": page.number,
                                "rect": [round(v, 2) for v in w.rect], "value": (w.field_value or "") if isinstance(w.field_value, str) else str(w.field_value)})
        text = "\n".join(texts)
        chars = len("".join(text.split()))
        return {"page_count": d.page_count, "encrypted": False, "text": text, "n_chars": chars,
                "image_area_ratio": round(img_area / page_area, 3) if page_area else 0.0, "n_drawings": draws,
                "is_scanned": chars < 20 * max(d.page_count, 1) and img_area > 0.5 * page_area,
                "form_fields": len(widgets), "widgets": widgets[:500], "page_sizes": [[round(p.rect.width, 2), round(p.rect.height, 2)] for p in d],
                "producer": (d.metadata or {}).get("producer"), "creator": (d.metadata or {}).get("creator")}


def pdf_page_pngs(data: bytes, dpi: int = 100, max_pages: int | None = None) -> list[bytes]:
    with pymupdf.open(stream=data, filetype="pdf") as d:
        return [p.get_pixmap(dpi=dpi, alpha=False).tobytes("png") for i, p in enumerate(d) if max_pages is None or i < max_pages]
