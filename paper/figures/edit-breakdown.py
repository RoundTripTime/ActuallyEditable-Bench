"""그림 4의 자료: 방식별 편집 시간(원본 321개에 대한 합계)을 변경, 추가, 삭제·페이지 설정으로 나눈 비율(%). MinerU2.5는 논문 비교 대상에서 제외.
GPT-6-Luna와 Docling은 ODT 직접 출력 라벨(png_150__gpt6luna_odt, vector_pdf__docling_odt).

입력 reports/experiments/main/edit_tasks.json (scripts/edit_breakdown.py; main과 같은 입력과 비용 코드로 다시 계산,
각 문서에서 합 = document_cost()를 확인함). 필드 labels.<label>.by_operation_share_pct.
변경 = 블록 유지·변경(기본 모양 설정과 일괄 변경 절감 포함), 추가 = 블록 추가, 삭제·페이지 설정 = 블록 삭제 + 페이지 설정.
실행: python3 paper/figures/edit-breakdown.py
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LABELS = [("oracle__template_odt", "원본"), ("vector_pdf__word_pdf", "Word"), ("vector_pdf__hwp_pdf", "한/글"),
          ("png_150__gpt6luna_odt", "GPT-6-Luna"), ("vector_pdf__docling_odt", "Docling"), ("png_150__image_only", "이미지")]
s = json.loads((ROOT / "reports/experiments/main/edit_tasks.json").read_text(encoding="utf-8"))
lines = ["name edit add del"]
for lab, name in LABELS:
    p = s["labels"][lab]["by_operation_share_pct"]
    # 소수 두 자리를 유지해 정수 표시 때 이중 반올림하지 않는다(예: 64.47 -> 64).
    lines.append(f"{{{name}}} {p['edit']:.2f} {p['add']:.2f} {p['delete'] + p['page']:.2f}")
    print(name, p)
out = Path(__file__).with_suffix(".dat")
out.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"-> {out.relative_to(ROOT)}")
