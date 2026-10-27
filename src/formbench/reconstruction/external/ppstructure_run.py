"""Run INSIDE the PaddleOCR venv (~/opt/venvs/paddle, paddleocr 2.10 / paddlepaddle 2.6.2 CPU): PP-Structure layout
analysis + OCR + table recognition on each page image, then its "layout recovery" assembles an editable Word document.
Usage: python ppstructure_run.py <list.tsv: src_pdf \t dst_html \t lang> <out.jsonl>
Formula recognition off (forms carry no equations) and oneDNN off (its shape cache grew past 8 GB per process on
varying page sizes, 2026-10-18). The page images are the png_150 files next to src_pdf (the image condition: no text layer); the DOCX is written next to
dst (same stem, .docx). Standalone (no formbench import)."""
import glob
import json
import os
import signal
import sys
import tempfile
import time
from copy import deepcopy
from importlib.metadata import version

PER_FILE_S = 900
LANG = {"ko": "korean", "ja": "japan", "zh": "chinese_cht", "en": "en"}
# PP-Structure's layout / table models exist for "ch" and "en" only: CJK documents use the "ch" layout + table models with
# the document language's recognition model; English documents the "en" models.
REC = {"korean": "korean/korean_PP-OCRv4_rec_infer", "japan": "japan/japan_PP-OCRv4_rec_infer",
       "chinese_cht": "chinese_cht/chinese_cht_PP-OCRv3_rec_infer"}


class _TooLong(BaseException):
    pass


def _too_long(signum, frame):
    raise _TooLong()


def main():
    import cv2
    from paddleocr import PPStructure, save_structure_res
    from paddleocr.ppstructure.recovery.recovery_to_doc import convert_info_docx, sorted_layout_boxes
    engines = {}
    out = open(sys.argv[2], "a", encoding="utf-8")
    for line in open(sys.argv[1], encoding="utf-8"):
        if not line.strip():
            continue
        src, dst, lang = line.rstrip("\n").split("\t")
        lg = LANG.get(lang, "en")
        t0 = time.time()
        signal.signal(signal.SIGALRM, _too_long)
        signal.alarm(PER_FILE_S)
        try:
            if lg not in engines:
                if lg in REC:
                    import paddleocr as _p
                    dict_dir = os.path.join(os.path.dirname(_p.__file__), "ppocr", "utils", "dict")
                    engines[lg] = PPStructure(recovery=True, lang="ch", show_log=False, use_gpu=False, formula=False, enable_mkldnn=False,
                                              rec_model_dir=os.path.expanduser(f"~/.paddleocr/whl/rec/{REC[lg]}"),
                                              rec_char_dict_path=os.path.join(dict_dir, f"{lg}_dict.txt"))
                else:
                    engines[lg] = PPStructure(recovery=True, lang="en", show_log=False, use_gpu=False, formula=False, enable_mkldnn=False)
            eng = engines[lg]
            pages = sorted(glob.glob(os.path.join(os.path.dirname(src), "png_150_p*.png")),
                           key=lambda p: int(p.rsplit("_p", 1)[1].split(".")[0]))
            with tempfile.TemporaryDirectory(prefix="fb_pps_") as td:
                all_res, img = [], None
                for k, pth in enumerate(pages):
                    img = cv2.imread(pth)
                    res = eng(img, img_idx=k)
                    save_structure_res(res, td, "doc", k)
                    if res:
                        all_res += sorted_layout_boxes(deepcopy(res), img.shape[1])
                if not all_res:
                    raise RuntimeError("no layout regions recognised")
                fallback = None
                try:
                    convert_info_docx(img, all_res, td, "doc")
                    with open(os.path.join(td, "doc_ocr.docx"), "rb") as fh, open(dst[:-5] + ".docx", "wb") as fo:
                        fo.write(fh.read())
                except Exception as ex:  # noqa: BLE001 - the official Word recovery fails on some merged tables
                    # the same pipeline's official Markdown recovery (tables kept as HTML) -> HTML document
                    fallback = f"docx recovery failed ({type(ex).__name__}: {str(ex)[:120]}); markdown recovery used"
                    import base64
                    import mimetypes
                    import re as _re

                    import markdown
                    from paddleocr.ppstructure.recovery.recovery_to_markdown import convert_info_markdown
                    convert_info_markdown(all_res, td, "doc")
                    md = open(os.path.join(td, "doc_ocr.md"), encoding="utf-8").read()
                    body = markdown.markdown(md, extensions=["tables"])

                    def embed(m):
                        pth = m.group(1) if os.path.isabs(m.group(1)) else os.path.join(td, m.group(1))
                        if not os.path.exists(pth):
                            return m.group(0)
                        mt = mimetypes.guess_type(pth)[0] or "image/jpeg"
                        return f'src="data:{mt};base64,{base64.b64encode(open(pth, "rb").read()).decode()}"'
                    body = _re.sub(r'src="([^"]+)"', embed, body)
                    open(dst, "w", encoding="utf-8").write(f'<!doctype html><html><head><meta charset="utf-8"></head><body>{body}</body></html>')
            rec = {"src": src, "status": "ok", "s": round(time.time() - t0, 2), "pages": len(pages)}
            if fallback:
                rec["fallback"] = fallback
        except _TooLong:
            rec = {"src": src, "status": "timeout", "reason": f"no result within {PER_FILE_S} s", "s": round(time.time() - t0, 2)}
        except Exception as e:  # noqa: BLE001
            rec = {"src": src, "status": "failed", "reason": f"{type(e).__name__}: {e}"[:300], "s": round(time.time() - t0, 2)}
        finally:
            signal.alarm(0)
        rec["tool"] = f"paddleocr {version('paddleocr')} PP-Structure layout recovery (paddlepaddle {version('paddlepaddle')}, CPU)"
        rec["ocr_lang"] = lg
        out.write(json.dumps(rec, ensure_ascii=False) + "\n")
        out.flush()


if __name__ == "__main__":
    main()
