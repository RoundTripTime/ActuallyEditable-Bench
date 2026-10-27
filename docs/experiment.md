# 실험 절차

원본 양식과 완성본의 쌍을 같은 경로로 ODT로 변환한 뒤, 원본을 LibreOffice로 렌더링한 PDF를 복원 입력과 시각 비교 기준으로 사용한다. 복원본과 원본을 각각 같은 완성본과 비교해 편집 시간을 추정한다. 평가 대상, 지표와 결과는 [README](../README.md)에 설명한다.

## 실행 환경

실험 기록에 남은 주요 워드프로세서 버전은 Microsoft Word 16.0 (빌드 16.0.14334), 한컴 한/글 13.0.0.564, LibreOffice 26.2.6.3 (빌드 `8221e31b3ac356a1623c672912a3d2b492f7e3d1`)이다. Word와 한/글은 Windows에서 실행했다. LibreOffice의 HWPX 가져오기에는 H2Orestart 0.7.14를 사용했다. 렌더링 결과는 설치된 글꼴에도 영향을 받으며, 원본 및 복원본의 공통 렌더링에는 Windows·한컴 글꼴을 사용했다.

버전은 각 문서의 `native.json`과 복원 결과 JSON에 기록된 실행 환경을 기준으로 한다. 다른 의존성은 [도구 버전 기록](../configs/tool_versions.lock)을 참고한다.

## 전처리

[preprocess.py](../scripts/preprocess.py)는 문서 형식별로 변환 경로를 준비한다. DOCX는 LibreOffice 또는 Word, HWPX는 LibreOffice와 H2Orestart 또는 한/글로 변환하고, ODT는 복사한다. 같은 쌍의 원본과 완성본에는 같은 경로를 적용한다.

LibreOffice 경로를 기본으로 삼는다. Word 또는 한/글 경로의 변환 손실이 1% 넘게 작고 원본 렌더링과의 시각 유사도가 기본 경로보다 0.02 넘게 낮지 않을 때만 해당 경로를 선택한다. 변환 손실은 변환한 원본과 완성본을 각각 변환 전 문서로 되돌리는 추정 편집 시간의 합이다. 선택 결과는 DOCX 208쌍 LibreOffice·27쌍 Word, HWPX 34쌍 LibreOffice·4쌍 한/글, ODT 48쌍 복사다. 변환에서 정보가 손실될 수 있다.

## 복원과 평가

Word, 한/글, Docling에는 디지털 PDF를 제공한다. GPT-6-Luna에는 150 dpi 페이지 이미지와 ODF 명세를 제공해 ODT를 생성한다. 이미지 대조군은 페이지 이미지를 문서에 삽입하며, 원본 대조군은 전처리된 원본 ODT를 사용한다.

복원 결과를 LibreOffice로 렌더링하여 시각 유사도와 페이지 넘침을 계산한다. 편집 시간은 원본과 복원본을 각각 완성본과 비교해 계산한다. 위치 허용 범위 6·24pt, 키 입력 시간 0.5·2배, 문자당 키 입력 1회 조건으로 민감도를 확인한다.

[run_experiment.sh](../scripts/run_experiment.sh)와 [evaluate.sh](../scripts/evaluate.sh)는 준비된 데이터와 외부 도구를 사용해 복원 및 평가를 실행한다. 전처리는 기존에 생성된 변환 파일과 과제 목록을 입력으로 사용한다. 원본과 파생 데이터는 `data/`에 있다. 경로와 실행 인자는 각 스크립트의 사용법을 참고한다.

## 보충 실험

GPT-6-Luna의 형식 오류로 실패한 17건에 파서 오류를 피드백해 복구한 실험과, 페이지 이미지에 텍스트 층이 있는 디지털 PDF를 추가 제공한 실험을 별도로 수행했다. 이 결과는 주 결과에 합산하지 않는다. 각각의 분석 자료는 `reports/experiments/gpt-repair/`와 `reports/experiments/gpt-pdf/`에 있다.
