# v2.5.0 R4 검증기록 — 특례도로 표 가독성 + 가로구역 선행분석

기준본: urban-strategy-v2.5.0-20260909-main-map-planning-preview-r3.zip
앱 표시 버전: v2.5.0 유지
작성일: 2026-09-09

## 1. 수정 범위

### A. 역세권 특례제도 도로 요약표 폭 조정
- `widthRoadSchemeSummary` 3열 표를 전용 grid로 분리.
- `frontageSchemeSummary`, `urbanRenewalFrontageSummary`도 동일한 3열 구조를 적용.
- 사업방식: 92~118px
- 판정: 58~72px
- 근거: 남은 폭 전체(`minmax(0,1fr)`)
- 근거 문장은 자연 줄바꿈을 허용하고 세로 한 글자씩 쌓이는 현상을 방지.
- 기존 4열 `stationSchemeJudgments` 표의 구조는 변경하지 않음.

### B. 제도별 가로구역 선행분석
- `runSiteReview()`에서 도형면적 계산 직후 `analyzeSchemeStreetBlocks()`를 실행.
- 연속지적 자동선정 및 역세권 경계 분석보다 가로구역을 먼저 산정.
- `runAllAutoAnalyses()`도 직접 호출되는 경우 가로구역을 첫 분석 단계로 실행.
- `runSiteReview()`에서 이미 산정한 경우 `skipStreetBlocks:true`로 중복 실행 방지.
- 가로구역 산정 결과는 이후 역세권·간선가로·사업별 Rule Module이 기존 구조대로 참조.

## 2. 보존 범위
- `app.py`: R3와 SHA-256 동일
- `regression_checks.py`: R3와 SHA-256 동일
- `road_shp_seoul.zip`: R3와 SHA-256 동일
- 사업별 법적 기준 및 판정식 자체는 변경하지 않음.
- 기존 메인 지도 레이어 토글/R3 도시계획 표시 기능 유지.
- 기존 PASS/REVIEW/FAIL 보정 로직 유지.

## 3. 검증 결과

### JavaScript 문법검사
- inline script 추출 후 `node --check`: PASS

### targeted_regression_checks.py
- 메인 지도 레이어 UI/표시도면: PASS
- 용도지역 92%, 시설 88%, z-order, 지적선 색상전환: PASS
- 도로 요약표 3열 폭 축소: PASS
- 가로구역이 연속지적보다 먼저 실행: PASS
- 가로구역이 역세권 경계보다 먼저 실행: PASS
- 가로구역 중복 실행 방지: PASS
- 도로 원천값 null → REVIEW 보정: PASS
- 역세권활성화 간선가로형 조회 실패 → REVIEW / 정상 0건 → FAIL: PASS
- direct 역세권 PASS와 가로구역 독립화: PASS
- 역세권복합개발 별도 가로구역 요건 보존: PASS
- 전체 targeted checks: PASS

### 기존 regression_checks.py
- 원본 검사 파일은 수정하지 않음.
- 현재 배포 ZIP 계열에는 `uq181_legal.zip`이 포함되어 있지 않아 `renewal spatial` 단계에서 FileNotFoundError로 중단됨.
- 이는 R4 수정으로 발생한 회귀가 아니라 R3 기준 패키지의 기존 의존파일 누락 상태와 동일함.

## 4. 비고
이번 수정은 화면 가독성과 분석 실행 순서만 조정한다. 가로구역의 제도별 4m/6m 기준, 역세권 범위, 간선도로 기준 및 다른 사업판정 Rule은 변경하지 않았다.
