# GPT PATCH NOTES — R43 사업구역 건축선 추출·연결 실험

기준본: `urban-strategy-v2.5.0-r42-project-boundary-building-line-ui.zip`
앱 내부 버전: `2.5.0` 유지

## 이번 수정
- 가로구역 계산은 r42와 바이트 단위 함수 해시가 동일하도록 동결했다.
- 사업구역은 가로구역과 별도 함수에서만 계산한다.
- 검토경계 주변 연속지적 필지는 선택 여부와 무관하게 토지대장 지목을 추가 조회하여 `도로` 필지를 확인한다.
- 사업구역 후보선은 지적 도로필지의 경계 중 대상지 중심 방향이 도로 밖으로 빠지는 **대지측 경계**를 사용한다.
- 각 후보선은 검토구역 외곽선과의 거리와 방향차를 비교해 평행성이 낮은 선을 제외한다.
- 검토구역 외곽 진행순서에 후보 건축선을 투영하고, 후보가 끊긴 구간은 인접 후보점끼리 직선 연결하여 폐합 폴리곤을 만든다.
- 공원·녹지·광장·주차장·철도 등 도시계획시설은 사업구역 경계 생성에 사용하지 않는다.
- ROAD_BT·계획도로는 사업구역 경계 생성이 아니라 진단 FACT로만 유지한다.
- 건축선 후보가 부족하거나 자기교차/중첩검증을 통과하지 못하면 `REVIEW`로 두고 검토요청지를 임시 표시한다.
- 검증 UI에 `건축선 후보 수 · 연결 수 · 검토경계 투영률`을 표시한다.

## 회귀 확인
- `buildProjectStreetBlockValidation()`은 r42와 SHA-256 동일: 가로구역 계산 미변경.
- 인라인 JavaScript `node --check` PASS.
- `python -m py_compile app.py regression_checks.py` PASS.
- `check_spatial_evidence_maps`, `check_r14_street_block_auto`, `check_r15_street_block_4m_conditional` PASS.
- 전체 회귀는 기존 기준본에 없는 사전협상 PDF를 요구하는 r10 검사에서 중단하며, 그 이전 항목은 모두 PASS.

---

# GPT PATCH NOTES — 사업구역·가로구역 완전 분리

기준본: `urban-strategy-v2.5.0-r34-project-area-boundary-network-fix.zip`
앱 내부 버전: `2.5.0` 유지

## 사용자 확정사항
- 카드 제목 **`사업구역·가로구역 검토`는 그대로 유지**한다.
- r32에서 확인된 **가로구역 추출 결과는 정상**이며 더 이상 사업구역 수정 때문에 건드리지 않는다.
- `사업구역`과 `가로구역`은 같은 FACT를 참조할 수 있지만 **서로의 계산결과를 입력으로 사용하지 않는다.**
- 사업구역은 별도 로직으로 하나의 통합된 폐합 폴리곤을 만든다.
- 쿨데삭/내부 가로망은 사업구역 안에 포함되어 외곽 사업구역계가 열린 홈 형태가 되지 않아야 한다.

## r34 오류
r34에서는 가로구역 분할결과(`rawBlocks`, `separators`)를 다시 이용해 사업구역을 만들었다. 이 때문에 사업구역 경계부 판정 수정이 가로구역 후보 자체에 영향을 주어, 실제 계획도로 2건으로 3개 가로구역이 보여야 하는 사례가 1개 가로구역으로 회귀했다.

## r35 구조

### 1. 가로구역 — r32 정상 로직 동결
`buildProjectStreetBlockValidation()`을 r32의 정상 추출 구조로 복구했다.

입력:
- 검토요청지
- 검토요청지와 교차하는 연속지적 도로필지
- `TL_SPRD_MANAGE ROAD_BT` 4m 이상 도로면
- 도시계획시설 도로·철도·하천·주차장·광장·공원·녹지·공공공지·학교

계산:
- separator 합집합 생성
- `검토요청지 - separator`로 가로구역 후보 생성

금지:
- 사업구역 결과를 가로구역 입력으로 사용하지 않음
- 가로구역 함수가 `project_area`를 생성하거나 수정하지 않음
- `rawBlocks + separator`를 재통합하지 않음

### 2. 사업구역 — 완전 독립 함수 신설
`buildIndependentProjectAreaCandidate()`를 신설했다.

입력:
- 검토요청지
- 연속지적 도로·철도용지 FACT
- ROAD_BT 현황도로 FACT
- 도시계획시설 FACT

사업구역 함수는 `street_blocks`, `projectStreetBlockValidation`, 가로구역 separator 결과를 참조하지 않는다.

계산:
1. 검토요청지와 원천 도로·시설의 공간관계를 직접 판정
2. 경계부를 따라가는 외곽 도로·시설만 사업구역 외곽 절단 후보로 분리
3. 내부 관통도로·쿨데삭·내부 가로망은 사업구역에서 빼지 않음
4. 외곽 FACT를 제외한 잔여 폴리곤이 여러 개면 가로구역 결과가 아니라 **선택된 비도로 필지의 실제 중첩면적 + 면적 + 요청지 중심 포함 여부**로 대표 통합폴리곤을 선택
5. 선택된 폴리곤을 독립 `project_area`로 저장

현재 55% 경계부 비율과 대규모 단부시설 1,500㎡ 기준은 법적 기준이 아니라 기존 플랫폼의 **공간형상 추출용 휴리스틱**이며 사업별 RULE에는 연결하지 않는다.

### 3. 호출 구조
도시계획시설/분석레이어가 갱신되면 아래 두 함수를 서로 독립적으로 호출한다.

- `buildProjectBoundaryCandidate()` → 사업구역 전용
- `buildProjectStreetBlockValidation(turf.feature(activeGeometry))` → 가로구역 전용

서로의 반환값을 전달하지 않는다.

### 4. UI
- 제목 유지
- 사업구역 상태: `독립 사업구역 추출 · 검증용`
- 범례: `사업구역·가로구역 독립연산`
- 가로구역 합계에는 r32와 동일하게 `분할시설` 면적을 표시
- 사업구역은 붉은 2점쇄선으로 별도 렌더하고 최상단 표시

## 회귀검증
- `python -m py_compile app.py regression_checks.py` PASS
- 인라인 JavaScript `node --check` PASS
- `check_spatial_evidence_maps` PASS
- `check_r14_street_block_auto` PASS — r35 분리 구조로 갱신
- `check_r15_street_block_4m_conditional` PASS
- 전체 회귀검사는 r9까지 PASS 후, 기준본에 없는 `도시계획변경 사전협상 운영지침(11차개정_2026.06.29).pdf`를 요구하는 기존 r10 검사에서 중단. 이번 수정과 무관.

## 실제 사례 재검증
1. 계획도로 2건 사례: **가로구역 3개**가 r32와 동일하게 복원되는지 확인
2. 11개 가로구역 사례: **11개 그대로 유지**되는지 확인
3. 사업구역: 가로구역 수와 무관하게 **붉은 사업구역 통합 외곽계 1개**가 별도로 생성되는지 확인
4. 쿨데삭 사례: 내부 막다른 도로 때문에 사업구역계가 안쪽으로 홈처럼 열리지 않고 **폐합된 폴리곤**인지 확인


## r36 · 상생주택 SHP 로더 복구 + cold-start 대기시간 보강
- 원인 확인: `biotope_seoul.zip`, `forest_classification_seoul_202608.zip` 파일 자체는 존재했으나, `app.py`의 SHP 로더가 호출하는 `_json_property()` 공통 함수가 누락되어 모든 레코드가 예외 처리 후 0건으로 버려지고 있었음.
- 수정: `_json_property()`를 공통 유틸로 복구하여 비오톱·산지구분도·기초단위구 DBF 속성을 JSON 안전형으로 변환.
- 로컬 실측: 비오톱 12,816건, 산지구분도 공익용 659건(임업용 292건 포함), 기초단위구 72,307건 로드 확인.
- Render cold start 여유 확보를 위해 `상생주택 보전환경` 분석 step timeout을 60초 → 180초로 상향.
- 가로구역/사업구역 로직은 수정하지 않음.

## r37 · 사업구역 검증도면 색분리
- 계산 로직은 변경하지 않고 `사업구역·가로구역 검토` 미니맵의 검증 시각화만 보강.
- 분홍면: 사업구역 독립연산에서 제외된 경계부 도로·시설(`boundary_cutters`).
- 파랑면: 가로구역 separator 중 사업구역 폴리곤 내부에 남는 내부가로망·분할시설. 이 교차연산은 화면 표시용이며 계산값에 환류하지 않음.
- 주황면: 기존 r32 동결 가로구역 후보(`blocks`) 그대로 표시.
- 붉은 2점쇄선: 독립 사업구역 폴리곤. 검은선: 사용자 검토요청지.
- 가로구역/사업구역 생성 함수와 면적 계산은 수정하지 않음.

## r38 - 내부가로망 검증표시 분리
- 사업구역/가로구역 계산 로직은 변경하지 않음.
- 검증도면의 파란 레이어가 모든 separator를 표시하던 문제를 수정.
- `buildInternalRoadNetworkDisplay()`를 추가하여 `cadastral_road`, `roadbt_road`, `planning_road`만 대상으로 내부가로망을 표시.
- 검토요청지 경계 6m 띠에 면적의 55% 이상이 놓인 도로는 경계부 도로로 보고 내부가로망 표시에서 제외.
- 경계에 접하더라도 내부로 깊게 들어오는 도로/쿨데삭은 내부가로망으로 유지.
- 진한 파란색은 표시 검증용이며 결과값을 사업구역 또는 가로구역 연산에 환류하지 않음.


## r39 — 가로구역 도시계획시설 도로 입력 검증 보완
- 가로구역 polygonize/difference 알고리즘은 변경하지 않음(r32 정상 구조 유지).
- `LT_C_UPISUQ151` 레이어라는 이유만으로 `기타도시시설`을 자동 `planning_road` separator로 넣던 경로를 차단.
- 가로구역용 별도 `streetBlockPlanningRoadEvidence()` 추가: 광로/대로/중로/소로 등급, 명시적 도로 명칭, UQS111~123/UQS190 도로시설 코드가 원속성에서 확인될 때만 자동 계획도로로 사용.
- 도로 레이어지만 세부유형이 모호한 시설은 자동 분할하지 않고 `시설분류 확인`으로 표시.
- 카드의 `계획도로` 건수는 사업구역 진단값이 아니라 실제 가로구역 separator로 채택된 계획도로 건수로 표시.
- 사업구역 독립 연산 및 r38 내부가로망 검증색 로직은 변경하지 않음.

## r40 — 가로구역 경계부 시설 1m 허용오차 분리
- 가로구역 polygonize/difference 본체는 변경하지 않음. 도로 분할과 도시계획시설 제척의 입력 조건만 분리.
- 계획도로는 r39의 도로근거 확인 + 4m 이상 조건을 그대로 사용.
- 도로 외 도시계획시설(주차장·공원·녹지·광장·공공공지·학교·철도·하천)은 검토요청지 경계 ±1.0m 띠에 실제 인접하는 경우에만 `경계부 시설` separator로 사용.
- 검토요청지 내부에 놓인 동일 시설은 가로구역에서 빼지 않고 `internalPlanningFacilities`로 보존하여 가로구역 면에 포함.
- 1.0m는 법적 기준이 아니라 사용자 구역계와 SHP 경계의 미세한 정합오차를 흡수하기 위한 GIS tolerance로 주석/화면 설명에 명시.
- 사업구역 독립연산과 r38 내부가로망 검증색 로직은 변경하지 않음.

### 검증
- 인라인 JavaScript 5개 블록 `node --check` PASS.
- 기존 `regression_checks.py`는 r9까지 PASS 후, 기준본에 없는 사전협상 PDF를 요구하는 기존 r10 검사에서 중단(이번 수정과 무관).
- 정적 확인: 내부 비도로 시설은 `addSeparator()` 호출 없이 보존되고, 경계 ±1m 인접 시설만 `boundary_*` separator로 전달됨.

## r41 · AI 종합분석 설명 레이어 (2026-09-04)
- 기존 사업방식 색상 요약과 독립 Rule 판정은 그대로 유지하고, 색상 요약 하단/상세검토 상단에 `AI 종합분석` 영역을 추가했다.
- 프론트는 `buildAiComprehensiveSummary()`에서 대상지 FACT, 공간 FACT, 사업별 PASS/CONDITIONAL/FAIL/REVIEW, gap, 계획가능용적률, 기존 추천순서만 JSON으로 축약한다.
- 원본 코드·전체 원시 GIS·법령 전문은 AI 요청에 전달하지 않는다.
- `/api/ai/comprehensive-analysis`는 설명 전용 endpoint이며 OpenAI Responses API를 사용할 때도 입력 JSON 밖의 법적 기준·수치·현황을 새로 판정하지 않도록 지시한다.
- REVIEW/UNKNOWN은 충족/미충족으로 단정하지 않고 추가 확인 필요로 표현하도록 고정했다.
- `OPENAI_API_KEY`가 없거나 AI 호출이 실패하면 기존 판정엔진 결과만 문장화한 `판정엔진 요약`으로 자동 fallback하며, AI 결과인 것처럼 표시하지 않는다.
- 앱 내부 버전은 v2.5.0을 유지한다.

## r42 — 사업구역 외곽선/표현 보완 (2026-09-04)
- 기준본: r41 AI 종합분석 통합본 유지.
- 가로구역 계산 로직은 수정하지 않음.
- 사업구역 계산은 계속 별도 독립 연산.
- 사업구역 경계 정제 입력을 외곽 도로-필지 접면 FACT로 제한:
  - 지적의 `도로`, 4m+ ROAD_BT, 도로유형이 확인된 계획도로만 외곽 경계 보강에 사용.
  - 공원·녹지·광장·주차장·철도·학교·하천·기타도시시설 등 비도로 기반시설은 사업구역 외곽선 생성에서 무시.
  - 내부 도로/쿨데삭은 사업구역 안에 포함.
- 외곽도로 데이터의 미세 단절은 1m topology tolerance로만 연결(법정 기준 아님).
- 최종 사업구역은 최외곽 shell을 사용해 내부 시설 때문에 hole이 생기지 않는 단일 폐합 폴리곤으로 유지.
- 검증도면 UI 선두께 조정:
  - 검토요청지 검정선 3.0 → 1.95 (약 65%)
  - 사업구역 붉은선 3.2 → 1.6 (50%)
  - 내부가로망 파란 경계선 1.8 → 0.9 (50%)

## r43 — 선택필지 구역계 갱신 시 안심주택 의료시설 재분석 호출 누락 보완 (2026-09-04)

### 원인
- `applySelectedParcelsAsBoundary()`에서 선택필지 병합 geometry로 `activeGeometry`를 갱신한 뒤 `analyzeLandLedger()`, `analyzeBuildings()`, `analyzeBuildingHub()`, `analyzeRoadAccess()`만 재호출하고 있었음.
- 이 경로에서 `analyzeSafeMedicalReference()`가 호출되지 않아, 필지를 대상지로 지정한 경우 `안심주택 의료시설 현황` 카드가 이전 구역계 결과 또는 `구역 설정 전` 상태에 남을 수 있었음.

### 수정범위
- `applySelectedParcelsAsBoundary()` 함수 내부의 기존 호출 순서·구조는 그대로 유지함.
- 기존 `Promise.allSettled([analyzeLandLedger(), analyzeBuildings()])` → `analyzeBuildingHub()` → `analyzeRoadAccess()` 뒤에 `await analyzeSafeMedicalReference();`를 추가함.
- 호출 완료 후 `renderSafeMedicalSpatialStatus();`를 명시적으로 실행해 새 선택필지 경계 기준 결과를 즉시 렌더함.
- `runAllAutoAnalyses()`, `runSiteReview()`, Draw CREATED/EDITED 핸들러, `safeAnalysisStep('의료시설', ...)`, 의료시설 350m 판정기준·대상시설·대표필지 산출 로직은 수정하지 않음.
- 이번 수정과 무관한 사업구역/가로구역, 역세권, 상생주택 보전환경, 기존 UI·도면·팝업 로직은 수정하지 않음.

### 회귀검증
1. `python -m py_compile app.py regression_checks.py` PASS.
2. 인라인 JavaScript 추출 후 `node --check` PASS.
3. 직접 그린 폴리곤의 기존 `검토하기` 의료시설 분석 경로는 코드 변경이 없어 기존 동작 유지 확인.
4. 선택필지 구역계 갱신 경로는 `analyzeRoadAccess()` 이후 `analyzeSafeMedicalReference()`와 `renderSafeMedicalSpatialStatus()`가 실행되도록 정적 호출순서 확인.
5. 선택필지 갱신 함수 단위 harness를 2회 연속 실행해 매 회 `analyzeSafeMedicalReference()`가 1회만 호출되고, `spSafeMedicalState`/`spSafeMedicalNearest` 대응 상태가 새 경계 기준 값으로 갱신되는 호출경로를 확인. 명시 렌더는 동일 상태의 재표시이며 네트워크 분석 중복 호출은 없음.
6. 수정 전후 `app.html` diff는 `applySelectedParcelsAsBoundary()` 내부 2줄 추가만 존재함을 확인. `runAllAutoAnalyses()`, `runSiteReview()`, Draw CREATED/EDITED 핸들러, `analyzeSafeMedicalReference()`, `renderSafeMedicalSpatialStatus()`, 사업구역/가로구역 및 상생주택 보전환경 함수 해시 동일.
7. 전체 `python regression_checks.py`는 safe medical API/boundary, spatial evidence maps 등 r9까지 PASS 후, 기준본에 포함되지 않은 사전협상 PDF를 요구하는 기존 r10 검사에서 중단됨. 이번 2줄 수정과 무관.

## r43 추가 — 도로·접도 실패와 노선형 상업지역·가로구역 실행 의존관계 제거 (2026-09-04)

### 원인
- `runAllAutoAnalyses()`에서 재개발·주거환경개선용 `analyzeRoadAccess()`의 `roadStep.status`가 `rejected`이면, 서로 독립적으로 원시 도로중심선 `TL_SPRD_MANAGE`/`LT_C_SPRD_MANAGE`를 호출하는 `analyzeActivationArterial()`과 `analyzeStreetBlock()`의 호출 자체를 생략하고 있었음.
- 따라서 재개발용 접도율 모듈의 `NO_DATA`/`rejected` 상태가 무관한 노선형 상업지역·가로구역 분석까지 `선행 도로 Fact 미확보`로 자동 `rejected` 처리하는 잘못된 실행 의존관계가 있었음.

### 수정범위
- 직전 배포본 `urban-strategy-v2.5.0-r43-safe-medical-selection-refresh.zip`을 기준으로 수정함.
- `runAllAutoAnalyses()` 내부의 `if(roadStep.status==='rejected'){...}else{...}` 게이트만 제거함.
- `results.push(roadStep);`은 그대로 유지하여 재개발용 도로·접도 결과의 `rejected`/`partial` 상태 표시는 계속 남김.
- 기존 `safeAnalysisStep('노선형 상업지역', ...)` 호출의 함수 본문·timeout 60000·classify 로직은 변경하지 않고 조건문 밖으로 이동함.
- 기존 `safeAnalysisStep('가로구역', analyzeStreetBlock, 90000, ...)` 호출의 함수·timeout·onTimeout·classify 로직은 변경하지 않고 조건문 밖으로 이동함.
- `if(activationArterialAnalysis.loaded){try{updateActivationArterialBlockLink();}catch...}`는 두 스텝 실행 뒤에 기존 그대로 유지함.
- `analyzeRoadAccess()`, `fetchRoadNetwork()`, `roadPolygonsFromCenterlines()`, `analyzeActivationArterial()`, `analyzeStreetBlock()`, `buildProjectStreetBlockValidation()` 내부 로직은 수정하지 않음.
- 접도율 판정의 숫자·기준·거리값은 추가/변경하지 않음.
- 이번 변경과 무관한 사업구역/가로구역 폴리곤 계산, r38~r42 검증도면 색분리, 상생주택 보전환경, 기존 UI·도면·팝업은 수정하지 않음.

### 회귀검증
1. `python -m py_compile app.py regression_checks.py` PASS.
2. `app.html` 인라인 JavaScript 추출 후 `node --check` PASS.
3. 수정 전후 `app.html` diff 확인: `runAllAutoAnalyses()`의 위 게이트 제거와 기존 else 내부 호출부의 들여쓰기 이동 외 변경 없음.
4. 정적 실행순서 확인: `도로·접도` 실행 → `results.push(roadStep)` → `노선형 상업지역` 실행 → `가로구역` 실행 → `updateActivationArterialBlockLink()` → `주변 공간현황` 순서를 유지함. `roadStep.status==='rejected'` 분기 및 `선행 ... 미확보 · 분석 미실행` 자동 상태주입 문구는 제거됨.
5. 독립 원자료 호출 확인: `analyzeActivationArterial()`과 `analyzeStreetBlock()` 모두 기존대로 `trySpatialLayerCandidates(['TL_SPRD_MANAGE','LT_C_SPRD_MANAGE'], ...)`를 자체 호출하며 해당 함수 내부는 수정 전후 동일함.
6. 보호대상 함수 정적 비교: `analyzeStreetBlock()`, `analyzeActivationArterial()`, `buildProjectStreetBlockValidation()`, `roadPolygonsFromCenterlines()`, `fetchRoadNetwork()`, `analyzeRoadAccess()` 본문은 수정 전후 동일함.
7. 서버 가로구역 독립산출 확인: 내장 SGIS 기초단위구가 존재하는 서울 테스트 geometry와 ROAD_BT=8m 중심선 1건을 `analyze_street_block()`에 직접 입력하여 `status=resolved`, `block` polygon 산출, `metadata.road_count=1` 확인. 재개발용 `analyzeRoadAccess()` 상태를 입력으로 요구하지 않음을 확인함.
8. `roadStep` 정상/실패 공통 경로 확인: `runAllAutoAnalyses()`에 더 이상 `roadStep.status`에 따른 노선형 상업지역·가로구역 호출 분기가 없으므로, 도로·접도 결과가 fulfilled/partial/rejected 어느 상태이든 두 독립 스텝이 각각 자기 `safeAnalysisStep` 결과를 생성함. 자동 `선행 도로 Fact 미확보` rejected 항목은 생성되지 않음.
9. 사업구역/가로구역 폴리곤 계산, 검증도면 색분리, 상생주택 보전환경 등 무관 코드의 회귀 여부는 수정 전후 파일 diff가 `runAllAutoAnalyses()` 게이트에만 한정됨을 통해 확인함.
10. 기존 전체 `python regression_checks.py` 실행은 종전 `r21 single boundary + sequential diagnostics` 검사에서 제거 대상 문구 `선행 ROAD_BT 미확보 · 분석 미실행`의 존재를 요구하는 구형 assertion 때문에 중단됨. 이번 요구사항과 정반대인 기존 테스트 기대값이며, 수정범위를 지키기 위해 `regression_checks.py`는 변경하지 않음. 그 이전 검사들은 PASS.
