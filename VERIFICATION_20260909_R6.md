# v2.5.0 R6 검증기록 — 한성대입구 305m 판정 + 가로구역 실행원인 진단

## 기준본
- `urban-strategy-v2.5.0-20260909-activation-characteristic-r5.zip`
- 앱 표시 버전은 `v2.5.0` 유지.

## 1. 역세권활성화 한성대입구 305m 오류
### 원인
- `activationStationCriterion()`의 250/350m 판정식은 이미 정상.
- 실제 실행에서는 단일노선 역이 `CONFIRMED_SINGLE_LINE`까지 승격되지 않는 경우가 있어 `line_data_complete=false`로 남음.
- 따라서 305m가 250~350m 잠재구간 REVIEW로 유지됨.

### 수정
- 백엔드 `_direct_station_line_probe()`에서 서로 다른 서울시 공식 조회경로를 교차확인:
  1. 전체 역-노선 기준자료(`SearchSTNBySubwayLineInfo` 계열)
  2. 역명 직접조회(`SearchInfoBySubwayNameService`)
- 두 경로가 동일한 단일노선을 반환하면 `transfer=false`, `transfer_status=CONFIRMED_SINGLE_LINE` 확정.
- 어느 경로에서든 2개 이상 노선이 확인되면 `CONFIRMED_TRANSFER` 우선.
- 한 경로만 성공하면 기존대로 UNRESOLVED/REVIEW 유지.
- 프론트에서도 `CONFIRMED_SINGLE_LINE`과 현재 공간보조자료의 2개 이상 노선이 충돌하면 자동 비환승 확정하지 않도록 안전가드 추가.

### 동적 검증
- 한성대입구 가상 공식자료: 전체노선표 4호선 + 역명 직접조회 4호선 → `CONFIRMED_SINGLE_LINE`, transfer=false: PASS
- 직접조회 mock reject + 전체노선표 4호선만 존재 → UNRESOLVED 유지: PASS
- 305.0m + 비환승 확정 + 지구중심 + 간선가로형 FAIL → 역세권 FAIL / 입지유형 FAIL: PASS

## 2. 가로구역 검토 미작동
### 원인 A — `/api/spatial/road-facts` 라우트 등록 누락
- 프론트는 해당 경로를 호출하지만 R5 `app.py`에 POST decorator가 없었음.
- R6에서 `@app.post('/api/spatial/road-facts')` 복구.
- 한성대입구 주변 테스트 geometry에서 내장 `road_shp_seoul.zip`의 TL_SPRD_MANAGE/ROAD_BT가 정상 반환됨(1,000건 이상).

### 원인 B — 필수 SGIS 기초단위구 번들 누락 (완전한 가로구역 산정의 현재 blocker)
- R5 ZIP에는 `basic_unit_seoul.zip`이 포함되어 있지 않음.
- 백엔드 가로구역 엔진은 SGIS 2025 기초단위구를 seed로 사용하며, 자료가 없으면 의도적으로 자동확정하지 않음.
- 과거 원본 회귀검사는 `basic_unit_seoul.zip > 10MB`, 72,307 features, 기준일 2025-06-30을 전제로 함.
- R14/R15 구형 도로면 polygonization 방식으로 임의 fallback하지 않음.

### R6 표시 보완
- 기초단위구 누락 시 상태를 FAIL 또는 단순 `미산정`으로 표시하지 않음.
- 상단: `확인필요 · SGIS 기초단위구 자료 미설치`
- 제도별 카드: `확인필요 · 기초단위구 자료`
- metadata/warnings에 `basic_unit_reason` 보존.

## 3. 검증 결과
- `python -m py_compile app.py`: PASS
- inline JavaScript `node --check`: PASS
- 기존 `targeted_regression_checks.py`: PASS
- R5 `targeted_regression_checks_r5.py`: PASS
- 신규 `targeted_regression_checks_r6.py`: PASS
- 원본 `regression_checks.py`: R5와 동일하게 배포 ZIP에 `uq181_legal.zip` 등 원본 기준자료가 빠져 있어 2번째 `renewal spatial` 단계에서 FileNotFoundError. 검사기 자체는 수정하지 않음.

## 4. 가로구역 완전복구에 필요한 파일
- SGIS 2025 `기초단위구 경계(시도)` SHP ZIP
- 권장 파일명: `basic_unit_seoul.zip`
- 좌표계: EPSG:5179
- 앱 루트 또는 data 폴더에 ZIP 그대로 배치.
- 해당 파일이 들어오면 기존 제도별 가로구역 엔진을 변경하지 않고 즉시 재검증 가능.

## 수정범위
- `app.py`: 단일노선 교차확인, road-facts POST route 복구
- `app.html`: 환승상충 안전가드, 기초단위구 누락 REVIEW 표시
- 기존 사업 판정기준, 특성관리지구 복원자료, 지도 UI, road 원본 데이터는 변경하지 않음.
