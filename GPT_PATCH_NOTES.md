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

## r44 — 안심주택 의료시설 대표필지 오프라인 연속지적 폴백

### 원인
- 배포환경에서 VWorld 연속지적 API(`_vworld_parcel_at_point` / `_vworld_parcel_by_address`)가 키·도메인·외부통신 문제로 대표필지를 확정하지 못하면 안심주택 의료시설 350m 분석이 `REVIEW`에 머물 수 있었다.
- 첨부 `medical_facility_cadastral_solution.zip`의 국가공간정보 연속지적도 서울 2020-12 스냅샷으로 91개 인정 의료시설 좌표를 검증한 결과 `facility_parcel_match.json` 기준 91/91 대표필지 PNU 매칭이 확인되었다.

### 해결전략
- VWorld 실시간 좌표 조회를 1순위, VWorld 주소 조회를 2순위로 그대로 유지했다.
- 두 VWorld 경로가 모두 `resolved`되지 않을 때만 오프라인 연속지적 스냅샷을 3순위 폴백으로 사용한다.
- 운영 서버에 서울 전체 934,780필지 SHP(200MB+)를 탑재하지 않고, 실증 완료된 91개 대표필지 geometry만 `data/safe_medical_parcels_202012.geojson`으로 사전 추출해 번들했다.
- 오프라인 폴백 결과는 `boundary_basis`/`source_type = OFFLINE_CADASTRAL_SNAPSHOT_202012`로 VWorld 결과와 명확히 구분하며, `boundary_note`에 `오프라인 연속지적도 스냅샷(기준일 2020-12), 최신 분할·합병 미반영 가능`을 기록한다.
- 오프라인 폴백까지 실패하면 기존처럼 `REVIEW`를 유지한다.
- 350m 기준, 인정 의료시설 종류, 대표필지 1필지 기준, 버퍼 산정 방식은 변경하지 않았다.

### 반영범위
- `app.py`: 오프라인 의료시설 대표필지 GeoJSON 색인/point-in-polygon 폴백 및 소스 라벨 분기만 추가.
- `data/safe_medical_parcels_202012.geojson`: 2020-12 서울 연속지적 SHP에서 `facility_parcel_match.json`의 91개 PNU만 사전 추출한 경량 폴백 데이터 추가.
- 사업구역/가로구역, 도로 FACT, 상생주택 보전환경, UI·도면·팝업은 수정하지 않았다.

### 회귀검증
1. `python -m py_compile app.py regression_checks.py` PASS.
2. 인라인 JavaScript `node --check` PASS.
3. `facility_parcel_match.json` 91개 좌표를 새 오프라인 폴백에 대조: 91/91 동일 PNU 매칭 PASS.
4. VWorld 좌표조회가 `resolved`일 때 오프라인 폴백이 개입하지 않고 기존 VWorld 결과를 우선하는 모의검증 PASS.
5. VWorld 좌표/주소 조회를 모두 실패로 강제했을 때 오프라인 폴백이 동작하고 `boundary_basis/source_type = OFFLINE_CADASTRAL_SNAPSHOT_202012`로 구분되는 것 확인 PASS.
6. 오프라인 91필지 GeoJSON 색인: 91건, 최초 로드 약 0.005초(현 분석환경 측정). 전체 서울 SHP 런타임 적재를 피하도록 구성함.
7. 기존 `regression_checks.py` 전체 실행은 기준본 `r43-road-gate-decoupled`에 남아 있는 과거 assertion(`선행 ROAD_BT 미확보 · 분석 미실행`)이 현재의 도로 게이트 분리 결정과 충돌하여 해당 지점에서 중단됨. 이번 의료시설 수정과 무관하며 테스트 파일은 변경하지 않음.

## r45 — 정확성 우선 검토 대기시간 확대 (2026-09-07)

### 원인
- Render 콜드스타트, 외부 공식 API 응답 지연, SHP 공간연산이 겹치는 대상지에서 기존 단계별 45~180초 제한이 먼저 만료되어, 실제 분석이 끝나기 전에 `시간초과`/`REVIEW`로 전환될 가능성이 있었음.
- 사용자는 분석 속도보다 정확성과 완료 가능성을 우선하며, 진행률 UI에서 실제 경과시간을 이미 확인할 수 있으므로 단계별 대기시간을 충분히 늘리는 방향으로 조정함.

### 수정범위
- 판정식·공간연산·API 호출순서·UI 구조는 변경하지 않고 `safeAnalysisStep()`의 검토 단계 timeout 값만 확대함.
- 기본 timeout: 60초 → 180초.
- 역세권 경계: 45초 → 120초.
- 연속지적: 60초 → 180초.
- 토지대장 / 건축물 공간: 60초 → 180초.
- 도시계획 GIS: 90초 → 240초.
- 상생주택 보전환경: 180초 → 300초.
- 정비사업 GIS / 개발사업 GIS: 60초 → 180초.
- 의료시설: 60초 → 240초.
- 건축HUB: 120초 → 300초.
- 도로·접도: 60초 → 180초.
- 노선형 상업지역: 60초 → 180초.
- 가로구역: 90초 → 240초.
- 주변 공간현황: 45초 → 120초.
- 브라우저 내부에서 즉시 계산되는 도형면적 5초 제한은 유지함.
- 저수준 API 요청 자체의 timeout, 판정기준, REVIEW/UNKNOWN 처리방식은 변경하지 않음.

### 회귀검증
1. `python -m py_compile app.py regression_checks.py` PASS.
2. 인라인 JavaScript 추출 후 `node --check` PASS.
3. 수정 전후 `app.html` diff 확인: `safeAnalysisStep` 기본값 및 각 검토 단계 timeout 숫자 변경만 존재하며 분석 함수 본문·호출순서·classify/onTimeout 로직은 변경 없음.
4. 기존 전체 `python regression_checks.py`는 measurement~progress truth 항목까지 PASS 후, 기준본에 이미 존재하는 구형 r21 assertion(`선행 ROAD_BT 미확보 · 분석 미실행`)에서 중단됨. 이번 timeout 변경과 무관함.

## r46 — 가로구역 r42 기준 복원·고정 (2026-09-07)

### 원인
- 이후 의료시설 폴백·검토시간 확대가 반영된 최신 작업본(r45)에는 `buildProjectStreetBlockValidation()` 본체는 r42와 동일하게 남아 있었으나, 가로구역 `analyzeStreetBlock()`의 도로 입력 경로가 r42-roadfact 분기와 달라져 `TL_SPRD_RW` 실폭도로 + `TL_SPRD_MANAGE ROAD_BT` 독립 도로 FACT를 소비하지 않는 상태가 되었다.
- 사용자 최종 결정에 따라 **가로구역 검토는 공유된 `urban-strategy-v2.5.0-r42-roadfact-rw-manage-branch`를 기준으로 고정**하며, 명시적 변경 요청 전까지 이 기준을 임의 변경하지 않는다.

### 수정범위
- 최신 r45를 기준으로 의료시설 오프라인 지적 폴백과 확대된 검토시간 등 이후 기능은 유지했다.
- 가로구역에 필요한 r42-roadfact 입력 체인만 복원했다.
  - 서버 `TL_SPRD_RW` + `TL_SPRD_MANAGE` 로컬 도로 FACT 조회 함수 및 `/api/spatial/road-facts` 엔드포인트 추가.
  - `data/road_shp_seoul/`의 r42 도로 원자료(RW/MANAGE SHP/SHX/DBF + DATA_SUMMARY) 복원.
  - 서버 `analyze_street_block()` / `_street_block_from_basic_units()`에 r42와 동일하게 `road_surface_features` 입력을 복원하고, ROAD_BT 폭원에 대응된 RW 실제 도로면을 barrier로 우선 사용하도록 복원.
  - 브라우저 `analyzeStreetBlock()`을 r42-roadfact 버전과 바이트 수준 동일하게 복원하고, 독립 도로 FACT 조회 helper(`independentRoadFactKey`, `fetchIndependentRoadFacts`, `independentRoadManageCandidate`)를 추가.
- **보호범위**
  - `buildProjectStreetBlockValidation()`은 r42/r45/r46 모두 동일 해시로 유지.
  - 사업구역 `buildIndependentProjectAreaCandidate()`는 r45 그대로 유지(이번 수정에서 변경하지 않음).
  - `analyzeActivationArterial()`, `runAllAutoAnalyses()`, `renderStreetBlockSpatialStatus()`도 r45 그대로 유지.
  - 의료시설 폴백, AI 종합분석, 상생주택 보전환경, 검토시간, 기존 UI·도면·팝업은 변경하지 않음.

### 회귀검증
1. `python -m py_compile app.py regression_checks.py` PASS.
2. 인라인 JavaScript 추출 후 `node --check` PASS.
3. 함수 해시 비교:
   - `buildProjectStreetBlockValidation()` r42 = r45 = r46 동일.
   - `analyzeStreetBlock()` r46 = r42-roadfact 동일.
   - `analyzeActivationArterial()`, `buildIndependentProjectAreaCandidate()`, `renderStreetBlockSpatialStatus()`, `runAllAutoAnalyses()` r46 = r45 동일.
4. 서울시청 인근 로컬 테스트 geometry에서 독립 도로 FACT 실연산 PASS:
   - `status=resolved`, RW 34건 / MANAGE 38건 / surface association 135건.
   - 동일 FACT를 `analyze_street_block()`에 전달해 `status=resolved`, 가로구역 후보 3개 산출, `road_surface_count=88`, `road_count=8` 확인.
   - 수치는 로컬 테스트 geometry 결과이며 법적 기준값이 아님.
5. 기존 `python regression_checks.py`는 measurement~progress truth까지 PASS 후, 기존 구형 r21 assertion `선행 ROAD_BT 미확보 · 분석 미실행`을 요구하는 지점에서 중단. 현재의 도로 게이트 분리 결정과 정반대인 과거 기대값이며 이번 가로구역 복원과 무관하여 테스트 파일은 변경하지 않음.

## r47 — 안심주택 학교 절대보호구역(UO101/UOA110) 공간 FACT·도면 추가 (2026-09-07)

### 원인
- 안심주택 운영기준의 학교 출입문 50m 배제항목이 기존에는 `학교 출입구 점자료 미연결` REVIEW로 남아 실제 대상지에서 자동 확인되지 않았다.
- 사용자가 제공한 국가공간정보 연속주제도 `LSMD_CONT_UO101_5174_11_202608`에는 서울 교육환경보호구역 3,767건이 있으며, MNUM 코드 기준 `UOA110` 절대보호구역 1,699건 / `UOA120` 상대보호구역 1,455건 / `UOA100` 기타 613건으로 확인됐다.
- 이번 안심주택 배제 FACT에는 법정 학교 출입문 50m 범위에 대응하는 `UOA110` 절대보호구역만 사용하고, 상대보호구역은 섞지 않는다.

### 수정범위
- 기준본은 `urban-strategy-v2.5.0-r46-streetblock-r42-restored`이며, r42 고정 가로구역 로직은 수정하지 않았다.
- `school_protection_seoul_202608.zip`을 번들하고 서버 시작 후 최초 호출 시 1회만 파싱하여 `UOA110` 1,699건을 WGS84로 변환·STRtree 색인한다.
- 신규 서버 FACT:
  - `/api/spatial/school-absolute-protection-data-status`
  - `/api/spatial/school-absolute-protection-intersections`
  - 대상지와 UOA110 절대보호구역의 실제 중첩면적·중첩률·중첩구역 수·학교명을 반환한다.
  - 별도 50m 버퍼를 임의 생성하지 않고 제공된 공시 절대보호구역 원본 도형을 그대로 사용한다.
- 공간현황 박스에 `안심주택 학교 절대보호구역 현황` 카드를 추가했다.
  - 절대보호구역 전체 도형과 대상지 중첩부를 별도 색상으로 도면화.
  - 대상지 중첩면적/중첩률/중첩구역 수/배제판정을 표시.
- 안심주택 FACT의 `학교 출입구 50m` 항목을 신규 UOA110 공간 FACT에 연결했다.
  - 공식 SHP 정상 + 중첩 없음: PASS.
  - 공식 SHP 정상 + 중첩 있음: 현재 입력 사업대상지에 배제공간이 포함된 것으로 FAIL 표시하고 중첩면적을 도면으로 제시.
  - SHP 미확보/조회 실패: 기존 원칙대로 REVIEW 유지.
- 서울도심 기본계획 배제범위 로직은 이번 패치에서 수정하지 않았다. 공식 범역/도로경계 소스 확정 후 별도 구현 대상으로 유지한다.

### 회귀검증
1. `python -m py_compile app.py regression_checks.py` PASS.
2. 인라인 JavaScript 추출 후 `node --check` PASS.
3. 제공 SHP 직접 로드: 전체 3,767건 중 UOA110 1,699건 색인 확인, invalid 1건은 `buffer(0)` 보정 후 geometry quality를 내부 기록.
4. UOA110 첫 번째 실제 도형을 대상지로 넣은 서버 함수 실연산: `present=true`, `zone_count=1`, 중첩면적 양수, 중첩률 100% 확인.
5. 보호기능 함수 해시 비교: `buildProjectStreetBlockValidation()`, `analyzeStreetBlock()`, `buildIndependentProjectAreaCandidate()`, `analyzeActivationArterial()`, `renderStreetBlockSpatialStatus()`가 r46과 동일함을 확인.
6. 기존 `python regression_checks.py`는 progress truth까지 PASS 후, 기준본에 남아 있는 과거 r21 assertion `선행 ROAD_BT 미확보 · 분석 미실행`에서 중단. 현재 도로 게이트 분리 결정과 충돌하는 기존 테스트이며 이번 학교 절대보호구역 변경과 무관하여 테스트 파일은 수정하지 않음.

## r48 — 사이트 분석 UI 재구성 (2026-09-07)

- 기준본: `urban-strategy-v2_5_0-r47-reorganized`
- 원칙: 기존 r47의 사업 판정 Rule, 가로구역/사업구역 연산, 데이터 경로는 변경하지 않음.
- 서비스 화면의 `공간현황 도면`을 `사이트 분석`으로 변경하고 다음 6개 흐름으로 재배치:
  1. 토지 / 용도지역 / 용도지구 / 도시계획시설
  2. 정비구역 현황 / 도시계획(개발)구역 / 문화재관련 현황 / 자연환경분석
  3. 건축물 / 노후도 / 건축물 용도 / 공장용도
  4. 역세권 / 가로구역 및 사업구역 / 간선도로 / 중심지
  5. 의료시설(안심주택) / 절대보호구역 50m(안심주택)
  6. 접도현황 / 접도진단
- 용도지구: 기존 VWorld `LT_C_UQ121·123·124·125·126·128·129·130` Fact를 별도 카드/도면으로 노출.
- 건축물 용도: 기존 건축HUB 주용도 Fact를 사용해 동수/비율과 안전한 필지-동 매칭 범위에서 도면화.
- 노후도: 기존 사업별 노후도 Fact는 그대로 유지하고, 공통 도면과 20년/30년 참고값만 별도 시각화.
- 문화재관련 현황: 면적/비율은 기존 `LT_C_UO301` 벡터 Fact 유지. 국가유산공간정보 WMS는 시각적 교차확인 전용 프록시를 추가했으며 WMS 응답은 PASS/FAIL 판정에 사용하지 않음. WFS 속성 연계는 후속 고도화로 보류.
- 접도현황: 기존 TL_SPRD_MANAGE `ROAD_BT` 원 Fact를 별도 도면으로 추가. 접도진단 Rule은 기존 로직 유지.
- 도면 유형: 일반 현황형 / 광역·입지형 / 진단형 3종으로 높이를 통일. 데스크톱 4열, 중간폭 2열, 모바일 1열 반응형.
- 회귀검사: `v2.5.0 regression checks: PASS`.
