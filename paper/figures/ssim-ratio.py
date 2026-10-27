"""그림 3의 점 자료: 복원에 성공한 네 방식(Word, 한/글, GPT-6-Luna, Docling; MinerU2.5 제외)의 (시각 유사도, 편집 시간 배율) 평가 결과.

입력 reports/experiments/main/rows.csv (run_id main, 실험 current), 필드 ssim_blur_native_pages, ratio.
대조군(oracle__template_odt, png_150__image_only)과 시각 유사도가 없는 복원 실패 행은 제외한다.
네 방식 1,267건(GPT-6-Luna 실패 17건 제외), 스피어만 -0.238. 실행: python3 paper/figures/ssim-ratio.py
"""
import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTROLS = {"oracle__template_odt", "png_150__image_only"}
DROPPED = {"vector_pdf__mineru", "png_150__gpt6luna", "vector_pdf__docling"}  # not in the paper set (MinerU2.5; HTML-route labels)
rows = csv.DictReader(open(ROOT / "reports/experiments/main/rows.csv", encoding="utf-8"))
pts = [(float(r["ssim_blur_native_pages"]), float(r["ratio"])) for r in rows
       if r["label"] not in CONTROLS | DROPPED and r["ssim_blur"] != ""]
out = Path(__file__).with_suffix(".dat")
out.write_text("ssim ratio\n" + "".join(f"{x:.4f} {y:.4f}\n" for x, y in sorted(pts)), encoding="utf-8")
print(f"{len(pts)} points -> {out.relative_to(ROOT)}")
