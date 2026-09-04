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

## r42-roadfact branch — TL_SPRD_RW 실폭도로 + TL_SPRD_MANAGE ROAD_BT 독립 FACT 결합 (2026-09-04)

### 원인 / 목적
- 재개발·주거환경개선 접도율의 기존 `analyzeRoadAccess()` / `fetchRoadNetwork()` 중심선 버퍼 모듈은 더 이상 개선하지 않는다.
- 가로구역과 역세권활성화 분석은 위 모듈의 성공 여부와 분리하여 서울 원본 도로자료를 직접 소비하도록 한다.
- `TL_SPRD_RW`는 실제 도로면 Polygon, `TL_SPRD_MANAGE`는 `ROAD_BT`·도로명·관리번호를 가진 중심선이며, 둘은 1:1이 아니라 다대다 관계로 보존한다.

### 수정 범위
1. `data/road_shp_seoul/`에 서울 전체 `TL_SPRD_RW`와 `TL_SPRD_MANAGE`의 SHP/SHX/DBF만 탑재했다. 접도분석과 무관한 `TL_SPRD_INTRVL`은 포함하지 않았다.
2. 서버에 `/api/spatial/road-facts`를 추가했다.
   - 요청 대상지 주변 bbox만 pyshp로 읽는다(cp949).
   - 원자료 CRS는 DATA_SUMMARY 기준 EPSG:5179로 처리한다.
   - RW invalid geometry는 `buffer(0)` 보정을 시도하고 품질 상태를 FACT에 남긴다.
   - RW↔MANAGE는 `intersects` 다대다 관계를 그대로 저장하고 대표 ROAD_BT 하나로 축약하지 않는다.
   - 매칭 0건, 복수 ROAD_BT, geometry repair 여부는 REVIEW 사유로 남긴다.
   - 임의 nearest 거리 threshold는 새로 만들지 않았다.
3. 가로구역 `analyzeStreetBlock()`은 독립 도로 FACT의 MANAGE `ROAD_BT`를 폭원 근거로 사용하고, 폭원이 확인된 구간은 RW 실제 도로면과 MANAGE의 관계구간을 barrier 형상으로 우선 사용한다. 새 RW FACT가 없을 때만 기존 중심선 기반 방식으로 fallback한다.
4. 역세권활성화 `analyzeActivationArterial()`은 재개발용 도로접도 결과가 아니라 독립 도로 FACT의 MANAGE를 직접 사용한다. 기존 노선형 상업지역 RULE 자체는 변경하지 않았다.
5. `runAllAutoAnalyses()`에서 재개발용 `roadStep` 실패가 위 두 독립 분석의 호출 자체를 막지 않도록 의존 게이트를 제거했다. `roadStep` 상태 자체는 기존처럼 결과 목록에 남는다.
6. `analyzeRoadAccess()`, `fetchRoadNetwork()`, `roadPolygonsFromCenterlines()` 내부 로직은 변경하지 않았다.

### 로컬 기술검증
- `python -m py_compile app.py regression_checks.py` PASS.
- 인라인 JavaScript 추출본 `node --check` PASS.
- 서울시청 인근 임의 소구역으로 `/api/spatial/road-facts` 핵심 함수를 로컬 실행:
  - 상태 `resolved`
  - RW 38건 / MANAGE 42건 / RW-MANAGE surface association 151건
  - RW unmatched 4건 / 복수 ROAD_BT RW 22건
  - 첫 호출 약 0.5초(로컬 환경 기준; Render 성능 보장값 아님)
- 동일 소구역에서 독립 road FACT를 `analyze_street_block()`에 전달해 `resolved`, 복수 가로구역 후보 산출을 확인했다.
- 기존 전체 `regression_checks.py`는 과거 `roadStep rejected → 가로구역/노선형 상업지역 분석 미실행` 동작을 강제하는 r21 assertion에서 중단한다. 이번 결정과 반대인 구 검사이므로 제품 로직을 되돌리지 않았으며, 해당 테스트 갱신은 별도 작업으로 남긴다.

### 배포서버 확인 필요
- 실제 대상지별 RW/MANAGE 후보 건수, 매칭 링크, ROAD_BT 혼재, REVIEW 사유를 확인한다.
- VWorld 연속지적과의 실제 접촉/면수 판정은 배포환경 API키·네트워크에서 별도 검증한다.
- 재개발·주거환경개선 접도율 NO_DATA/rejected는 이번 변경의 성공/실패 판단 대상이 아니다.
