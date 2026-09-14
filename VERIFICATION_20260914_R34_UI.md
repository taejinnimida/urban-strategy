# VERIFICATION 20260914 R34 UI

## 기준본
- `urban-strategy-v2.5.0-20260912-fact-path-normalization-r33-PATCH.zip`
- 앱 내부 버전: **v2.5.0 유지**

## 수정 목적
준공업지역 `정비·개발` 전용 패널에서 사업명과 설명문이 유사한 크기·굵기로 표시되어, 어떤 사업을 검토하는 카드인지 빠르게 식별하기 어려운 UI 문제를 개선했다.

## 수정 범위
`app.html`의 준공업지역 전용 카드 표시부만 수정했다.

- 사업명: 11.5px / 900 굵기 / 진한 보라-남색 / `word-break: keep-all`
- 사업명 앞: 사업유형을 빠르게 구분하는 20px 아이콘 배지 추가
- 설명문: 작은 회색 보조문구로 계층 분리
- 판정상태: 카드 하단 배지 유지
- hover: 테두리·그림자만 가볍게 강조
- 긴 사업명은 단어 단위 줄바꿈을 우선하도록 처리

## 보존 확인
아래는 변경하지 않았다.

- `renderSemiindustrialDevelopmentPanel()`의 카드 생성 순서
- 카드별 `status`, `label`, `note`, `onclick`
- `semiindustrialExistingSchemeCard()`의 판정상태 계산
- FACT / RULE / RESULT 로직
- 준공업지역 활성화 조건
- 기존 팝업 연결
- 사업별 수치기준
- 다른 사업군 UI

## 코드 변경점
1. 준공업 카드 전용 CSS 계층 강화
2. `semiindustrialCardIcon(title)` 표시 전용 helper 추가
3. 카드 렌더 마크업을 `아이콘 + 큰 사업명 / 설명 / 상태배지` 구조로 변경

`semiindustrialCardIcon()`은 표시용 문자열만 반환하며 판정값이나 사업선택 로직에는 사용하지 않는다.

## 검증
- `node --check` — **PASS** (app.html inline JavaScript)
- 원본 R33 대비 diff 확인 — 변경은 준공업 카드 CSS, 표시용 icon helper, 카드 렌더 마크업에 한정
- R33 ZIP에 `app.py` 및 기존 회귀 스크립트가 포함되어 있지 않아 백엔드/실서버 회귀는 이번 패치에서 실행하지 않음

## 배포 후 확인
1. 준공업지역 대상지에서 각 카드 사업명이 설명보다 먼저 읽히는지
2. `도심공공주택복합 · 주거산업융합` 등 긴 제목이 글자 단위로 과도하게 깨지지 않는지
3. 카드 클릭 시 기존 상세검토/선택근거 팝업이 동일하게 열리는지
4. PASS/FAIL/REVIEW/INFO 상태배지가 종전 색상과 값으로 유지되는지
5. 1000px 이하 화면에서 2열 배치가 유지되는지
