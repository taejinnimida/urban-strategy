# R13 인수인계 — CORE FACT GATE

## 1. 기준본과 목적

- 기준본: `urban-strategy-v2.5.0-20260921-r40-REGULATORY-WIP-REGULATION-3PANEL-DISASTER-R12.zip`
- 신규본: `urban-strategy-v2.5.0-20260921-r40-REGULATORY-WIP-CORE-FACT-GATE-R13.zip`
- 목적: 토지·건축물·도시관리계획 필수 기본현황이 완결되기 전 사업판정·추천·밀도·AI 분석이 실행되는 경로를 차단한다.

## 2. 실제 수정한 주요 함수·구조

### 서버 `app.py`

- 신규 입력모델: `PlanningLayersInput`
- 신규 엔드포인트: `POST /api/spatial/planning-layers`
- 신규 처리: `_planning_geometry_signature()`, `_planning_cache_get()`, `_planning_cache_put()`, `_fetch_planning_layer_once()`, `_planning_layer_result()`
- 건축HUB 보강: `building_hub_title_batch()`, `building_hub_floor_batch()`의 PNU별 상태 반환

### 브라우저 `app.html` / `inline_scripts.js`

- 도시관리계획: `fetchPlanningBatch()`, `updatePlanningCompleteness()`, `recordPlanningLayerResults()`, `planningLayerDisplayStatus()`, `analyzePlanningGIS()`
- 토지: `fetchLandLedgerStatus()`, `analyzeLandLedger()`, `officialSmallParcelRatio()`
- 건축물: `analyzeBuildings()`, `analyzeBuildingHub()`, `thresholdStatus()`
- 완료 게이트: `coreFactReadiness()`, `renderCoreFactPending()`, `runSchemeChecksWhenCoreReady()`
- 판정 후속 차단: `scheduleAiComprehensiveAnalysis()`, `updateCandidateSchemes()`, `renderPriorityPreview()`

## 3. 도시관리계획 23개 레이어 상태관리

각 레이어에 `SUCCESS_DATA`, `SUCCESS_EMPTY`, `ERROR`, `NOT_RUN`을 저장한다. 레이어별로 건수, 오류, 시도횟수, 경로, 소요시간, 캐시 사용 여부를 함께 보존한다.

집계값은 `requiredLayerCount`, `completedLayerCount`, `successDataLayerCount`, `successEmptyLayerCount`, `failedLayerIds`, `notRunLayerIds`, `planningComplete`로 관리한다. 23개 전부가 `SUCCESS_DATA` 또는 `SUCCESS_EMPTY`일 때만 `planningComplete=true`다. `ERROR`의 빈 배열은 중첩 없음으로 표시하지 않는다.

## 4. VWorld 요청경로와 동시호출 제한

- 종전: UQ111만 서버, 나머지 22개는 브라우저 JSONP 직접호출
- R13: 23개 모두 `/api/spatial/planning-layers` 서버 중계
- 브라우저 묶음: 최대 5개, 묶음 간 순차실행
- 조회순서: 용도지역 → 시설 151~159 → 용도지구 → 지구단위계획 → 국가유산 → 개발행위제한
- 서버 내부: 요청당 작업자 최대 2개
- 서버 프로세스 전체: 공통 `BoundedSemaphore(2)`로 VWorld 호출 최대 2개
- 경로: `_vworld_get()`의 direct → VWorld 공식 proxy fallback 재사용

재시도는 네트워크·시간초과·429·5xx·일시응답만 최대 2회 실시한다. 간격은 1.5초, 4초이며 429의 `Retry-After`를 우선한다. 고정 4xx·인증·요청오류는 즉시 `ERROR`다.

## 5. 기본현황 완료 게이트

`coreFactReadiness()`는 다음 다섯 그룹을 모두 확인한다.

1. 연속지적: 정상조회 및 선택 PNU 1개 이상
2. 토지대장: 선택 PNU 전체 공식면적 확보
3. 건축물 공간: `SUCCESS_DATA` 또는 `SUCCESS_EMPTY`
4. 건축HUB 표제부: 선택 PNU 전체가 `SUCCESS_DATA` 또는 `SUCCESS_EMPTY`
5. 도시관리계획: 23개 레이어 완결

하나라도 미완료이면 기존 결과를 비우고 `사업판정 대기`로 전환한다. `runAllSchemeChecks()`의 본문은 byte-identical로 보존했으며, 전역 호출은 완료 게이트 래퍼를 통하도록 연결했다. 추천·우선순위·밀도추천·AI 종합분석도 같은 게이트를 확인한다.

## 6. 토지대장과 과소필지

- PNU별 `SUCCESS_DATA`, `ERROR`, `NOT_RUN` 상태와 오류원인을 보존한다.
- 공식면적 일부 누락 시 `small_parcel_count` 입력값을 비운다.
- 연속지적 도형면적은 `_preliminary_area_m2`와 `preliminary_small_count`로만 보존한다.
- `analysisState.quality.small === 'OFFICIAL'`일 때만 과소필지 비율을 Rule Module에 전달한다.
- `MIXED`, `NONE`, `ERROR`에서는 30%·40%·50% 기준이 `REVIEW`가 된다.

## 7. 건축물 공간·건축HUB·노후도

- `analyzeBuildings()`는 실패 시 화면만 갱신하고 끝내지 않고 오류를 상위로 재전달한다.
- 정상 0건은 `SUCCESS_EMPTY`, 실패는 `ERROR`다.
- 건축HUB 표제부·층별개요는 요청·성공·정상빈값·실패 PNU와 PNU별 상태를 반환한다.
- 표제부 PNU 누락 시 모집단 불완전으로 모든 노후도 임계값 판정을 `REVIEW` 처리한다.
- 모집단이 완전하고 사용승인일만 일부 미확인인 경우에는 기존 `boundedRatio()` 하한·상한 판정을 유지한다.
- 층별개요 불완전은 층별개요 의존 기준에만 영향을 주며 일반 노후도 모집단과 분리한다.
- 최종 Fact Store의 building 객체에 모집단·승인일·층별개요 완전성 필드를 추가한다.

## 8. 실패자료만 재조회와 정상 캐시

- 도시관리계획: `ERROR` 레이어만 재조회
- 토지대장: 같은 geometry signature의 `SUCCESS_DATA` PNU 재사용, 실패 PNU만 호출
- 건축HUB: 같은 geometry signature의 `SUCCESS_DATA`·`SUCCESS_EMPTY` PNU와 레코드 유지, 실패 PNU만 호출
- 서버 도시관리계획 캐시: geometry signature + layer ID, 정상결과만 최대 300개·10분 보존
- `ERROR`는 기존 정상결과를 덮어쓰지 않는다.
- 구역계가 바뀌면 이전 geometry signature의 자료를 재사용하지 않는다.

## 9. 기존 기능 보존

다음 핵심 함수는 R12 해시와 동일하다.

- `densityForScheme()`
- `runAllSchemeChecks()`
- `checkActivationFromFacts()`
- `analyzeSchemeStreetBlocks()`
- `analyzeRoadAccess()`
- `buildSiteFactStore()`

사업별 법적 수치, PASS·FAIL·REVIEW Rule, 밀도, 순위, 공공기여, 접도, 가로구역, 사업구역, 역세권, 의료시설, CURRENT PLAN, 규제 3종, 자연재해, 공시지가·사업성 보정계수는 변경하지 않았다.

## 10. 저장용량

서울 전역 원자료나 대형 공간파일을 추가하지 않았다. 추가 저장은 코드·검사·문서뿐이다. 런타임 캐시는 제한된 서버 메모리 캐시이며 Render 재시작 시 사라져도 기능상 오류가 없다.

## 11. 배포 후 확인사항

- Render의 `VWORLD_API_KEY`, 등록 도메인, `BUILDING_HUB_API_KEY` 실제 응답
- 23개 레이어의 direct/proxy 경로, 429 `Retry-After`, 실측 소요시간
- 나대지의 LT_C_SPBD 및 건축HUB `SUCCESS_EMPTY` 실제 응답
- 일부 PNU·레이어 장애 후 재조회 버튼이 실패대상만 호출하는지 브라우저 Network 탭 확인
- 동시 사용자 환경에서 전역 VWorld 동시호출 2개 제한과 콜드스타트 체감시간

