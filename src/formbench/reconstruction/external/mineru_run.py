"""Run INSIDE the MinerU venv (~/opt/venvs/mineru, mineru 2.7.6): PDF -> MinerU2.5 VLM (vlm-transformers backend)
-> Markdown with HTML tables -> HTML with embedded images, one file per input.
Usage: python mineru_run.py <list.tsv: src_pdf \t dst_html \t lang> <out.jsonl>
The language column only selects the OCR model of MinerU's helper pipeline; the VLM reads the page images itself.
Standalone (no formbench import) so the tool's dependencies never mix with the benchmark's."""
import base64
import json
import mimetypes
import os
import re
import signal
import sys
import tempfile
import time
from importlib.metadata import version

LANG = {"ko": "korean", "ja": "japan", "zh": "chinese_cht", "en": "en", "ne": "devanagari"}


PER_FILE_S = int(os.environ.get("FORMBENCH_TOOL_TIMEOUT_S", "600"))


class _TooLong(BaseException):
    pass


def _too_long(signum, frame):
    raise _TooLong()


def main():
    import markdown
    from mineru.cli.common import do_parse
    out = open(sys.argv[2], "a", encoding="utf-8")
    for line in open(sys.argv[1], encoding="utf-8"):
        if not line.strip():
            continue
        src, dst, lang = line.rstrip("\n").split("\t")
        t0 = time.time()
        signal.signal(signal.SIGALRM, _too_long)
        signal.alarm(PER_FILE_S)
        try:
            with tempfile.TemporaryDirectory(prefix="fb_mineru_") as td:
                name = "doc"
                do_parse(td, [name], [open(src, "rb").read()], [LANG.get(lang, "en")], backend="vlm-transformers",
                         f_draw_layout_bbox=False, f_draw_span_bbox=False, f_dump_middle_json=False, f_dump_model_output=False,
                         f_dump_orig_pdf=False, f_dump_content_list=False)
                md_files = [os.path.join(r, f) for r, _, fs in os.walk(td) for f in fs if f.endswith(".md")]
                if not md_files:
                    raise RuntimeError("MinerU produced no Markdown")
                md_path = md_files[0]
                md = open(md_path, encoding="utf-8").read()
                body = markdown.markdown(md, extensions=["tables"])
                base = os.path.dirname(md_path)

                def embed(m):
                    p = os.path.join(base, m.group(1))
                    if not os.path.exists(p):
                        return m.group(0)
                    mt = mimetypes.guess_type(p)[0] or "image/jpeg"
                    return f'src="data:{mt};base64,{base64.b64encode(open(p, "rb").read()).decode()}"'
                body = re.sub(r'src="([^"]+)"', embed, body)
                open(dst, "w", encoding="utf-8").write(f'<!doctype html><html><head><meta charset="utf-8"></head><body>{body}</body></html>')
            rec = {"src": src, "status": "ok", "s": round(time.time() - t0, 2)}
        except _TooLong:
            rec = {"src": src, "status": "timeout", "reason": f"no result within {PER_FILE_S} s", "s": round(time.time() - t0, 2)}
        except Exception as e:  # noqa: BLE001
            rec = {"src": src, "status": "failed", "reason": f"{type(e).__name__}: {e}"[:300], "s": round(time.time() - t0, 2)}
        finally:
            signal.alarm(0)
        rec["tool"] = f"mineru {version('mineru')} (vlm-transformers, MinerU2.5 VLM)"
        rec["ocr_lang"] = LANG.get(lang, "en")
        out.write(json.dumps(rec, ensure_ascii=False) + "\n")
        out.flush()


if __name__ == "__main__":
    main()
