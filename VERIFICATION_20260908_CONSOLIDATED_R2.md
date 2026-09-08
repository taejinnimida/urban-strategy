# 정비사업플랫폼 통합 재정리 검증 — 2026-09-08 R2

## 기준본
- `urban-strategy-v2.5.0-20260908-review-gates-final.zip`
- 앱 표시 버전 `v2.5.0` 유지
- 프로젝트 원칙: 데이터/API 실패와 법적 미충족 분리, 사업별 Rule 독립, 수정범위 외 기능 보존

## 이번 정리 범위
### A. 메인 검토지도
- 지도 위 우측 고정 레이어 박스 제거
- `지도에서 구역계 그리기` 옆 작은 `도시계획 레이어 ▾` 접이식 패널로 이동
- 기본은 접힘: 지도 높이/위치 변화 없음
- `도시계획 전체 ON` 버튼 추가
- 레이어 순서 고정: Satellite 200 → 용도지역 320 → 지적 330 → 도시계획시설 340
- 용도지역 기본 투명도 92%, 경계선 1.7px로 강화
- 도시계획시설 기본 투명도 88%, 진한 청색 경계 2.2px로 강화
- 지적선: 용도지역 ON이면 검정, 용도지역 OFF + 항공사진 ON이면 흰색
- 각 투명도 0~100%, 0이면 완전 비표시
- 새 WMS/API fetch 없음. 기존 `compactParcelBaseFeatures()`, `planningAnalysis.zoning`, `planningAnalysis.facilities` 재사용
- 현재 적재 건수를 패널 하단에 표시

### B. PASS / REVIEW / FAIL 재확인 및 회귀고정
1. `schemeRoadEvidenceFacts()`
   - 성장잠재권: `maxWidth` 또는 `road35Perimeter` 미확보 → REVIEW
   - 장기전세: 폭원/접면/교차거리 미확보 → REVIEW
   - 도심복합 성장거점형: 원천 최대폭 미확보 → REVIEW
   - 도심복합 주거중심형: 면적별 도로기준/최대폭/6m 블록 미확보 → REVIEW
2. `analyzeActivationArterial()`
   - Promise reject / VWorld 조회 실패 → REVIEW
   - 정상조회 완료 후 도로 0건 또는 용도지역 0건 → FAIL 유지
   - catch 예외 → REVIEW + `자동분석 오류 · 공개 GIS 자동판정 확인불가`
   - 화면 상태 라벨 REVIEW → `확인필요`
3. `activationStationCriterion()`
   - directStatus PASS → overall PASS. 가로구역 비율 미확보가 역세권 범위 PASS를 끌어내리지 않음
   - 가로구역 포함률은 `block`에 독립 유지
   - `checkStationComplexFromFacts()`의 별도 가로구역 점유요건 유지

## 검증결과
### 전용 회귀검사
`python targeted_regression_checks.py`
- 전 항목 PASS
- mock reject → activation arterial REVIEW 확인
- 정상조회 + 도로 0건 → FAIL 확인
- 정상조회 + 용도지역 0건 → FAIL 확인
- 4.6m + 가로구역 미확보 → direct PASS / block REVIEW / overall PASS 확인
- 280m + 확정 350m → PASS 확인
- 280m + 확정 250m → direct/overall FAIL, block 60%는 독립 PASS 확인
- 400m + block 60% → overall FAIL, block 독립 PASS 확인

### 문법·구조
- inline JavaScript `node --check` PASS
- HTML id 중복 없음
- `app.py` 기준본과 SHA256 동일
- `road_shp_seoul.zip` 기준본과 SHA256 동일
- `regression_checks.py` 기준본 원본과 SHA256 동일 (검사기를 수정해서 통과시키지 않음)

### 브라우저 레이아웃 하네스
- Chromium headless + 실제 CSS/DOM으로 접힌/펼친 상태 확인
- 접힌 상태: 작은 `도시계획 레이어` 버튼만 표시
- 펼침 패널: absolute popover이므로 지도 document top/height 변화 없음

## 기존 regression_checks.py 전체 실행 상태
현재 기준 ZIP 자체에 `uq181_legal.zip`이 포함되어 있지 않아 원본 `regression_checks.py`는 두 번째 검사인 `renewal spatial`에서 아래와 같이 중단됩니다.

`FileNotFoundError: uq181_legal.zip`

이 실패는 이번 수정 코드 때문이 아니라 **기준 ZIP의 테스트 의존자료 누락**입니다. 원본 검사기를 변경하지 않았습니다. 전체 회귀검사를 완주하려면 배포 루트의 기존 `uq181_legal.zip`, `uq120_project.zip`, stations/centers 등 기준 데이터 파일과 함께 실행해야 합니다.
