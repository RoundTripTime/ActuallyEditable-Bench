"""Run INSIDE the Docling venv (~/opt/venvs/docling): PDF -> HTML with embedded images, one file per input.
Usage: python docling_run.py <list.tsv: src_pdf \t dst_html \t lang> <out.jsonl>
OCR: EasyOCR with the document language (the same information the in-house baselines receive).
Standalone (no formbench import) so the tool's dependencies never mix with the benchmark's."""
import json
import signal
import sys
import time
from importlib.metadata import version

PER_FILE_S = 600


class _TooLong(BaseException):
    pass


def _too_long(signum, frame):
    raise _TooLong()


def main():
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import EasyOcrOptions, PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling_core.types.doc import ImageRefMode
    convs = {}

    langs = {"ko": ["ko", "en"], "ja": ["ja", "en"], "zh": ["ch_tra", "en"], "ne": ["ne", "en"], "en": ["en"]}

    def conv(lang: str):
        if lang not in convs:
            opt = PdfPipelineOptions()
            opt.do_ocr = True
            opt.ocr_options = EasyOcrOptions(lang=langs.get(lang, ["en"]))
            opt.do_table_structure = True
            opt.generate_picture_images = True
            opt.images_scale = 2.0
            convs[lang] = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opt)})
        return convs[lang]
    out = open(sys.argv[2], "a", encoding="utf-8")
    for line in open(sys.argv[1], encoding="utf-8"):
        if not line.strip():
            continue
        src, dst, lang = line.rstrip("\n").split("\t")
        t0 = time.time()
        signal.signal(signal.SIGALRM, _too_long)
        signal.alarm(PER_FILE_S)
        try:
            res = conv(lang).convert(src)
            res.document.save_as_html(dst, image_mode=ImageRefMode.EMBEDDED)
            rec = {"src": src, "status": "ok", "s": round(time.time() - t0, 2)}
        except _TooLong:
            rec = {"src": src, "status": "timeout", "reason": f"no result within {PER_FILE_S} s", "s": round(time.time() - t0, 2)}
        except Exception as e:  # noqa: BLE001
            rec = {"src": src, "status": "failed", "reason": f"{type(e).__name__}: {e}"[:300], "s": round(time.time() - t0, 2)}
        finally:
            signal.alarm(0)
        rec["tool"] = f"docling {version('docling')} (docling-ibm-models {version('docling-ibm-models')}, easyocr {version('easyocr')})"
        rec["ocr_lang"] = langs.get(lang, ["en"])
        out.write(json.dumps(rec, ensure_ascii=False) + "\n")
        out.flush()


if __name__ == "__main__":
    main()
