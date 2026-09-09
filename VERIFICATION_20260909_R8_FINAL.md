# R8 FINAL 검증기록 — 2026-09-09

## 1. 기준 및 최종 선택
- 코드 기준: `R6-FIX2`
- 선택 병합: R7의 `streetBlockRuntimePreflight()` / `markSchemeStreetBlocksUnavailable()` 및 `/health` 기준자료 진단
- 의도적 미채택: R7의 `streetBlockPromise` / `streetBlockBackground` 백그라운드 병렬화

이유: 원 코드에 이미 "대형 공간자료·외부 API는 순차 실행한다. 작은 Render 인스턴스에서 SHP 2종 동시 적재로 HTML 오류/프로세스 재시작 가능성을 낮춘다"는 운영결정이 존재한다. 가로구역도 ROAD_BT/계획도로/비건축공간을 함께 다루는 무거운 분석이므로, 누락자료의 지연은 preflight로 제거하되 대형 분석의 동시 실행은 피하는 쪽을 최종 채택했다.

## 2. 수정내용
### 필지선택 경로
- `clearBoundaryAnalysisForNewGeometry({keepParcelSelection:true})`
- 선택필지 원장은 보존하고 이전 대상지 Fact만 초기화
- 구역계 확정 시 토지/건축/HUB/도로/의료 분석을 선실행하지 않음
- `검토하기`에서 전체 분석 실행

### 가로구역
- 전체 분석의 첫 단계에서 순차 `await`
- `/health` 사전점검 최대 5초
- `street_block_basic_unit_configured=false`이면 VWorld 다중조회 및 백엔드 가로구역 POST를 시작하지 않음
- 4개 제도 가로구역을 `REVIEW`로 기록
- 진행상태도 `rejected`가 아니라 `partial`로 표시
- 그 뒤 연속지적/역세권/나머지 Fact 분석은 정상 계속
- `/health` 자체 확인 실패 시 자료 누락으로 단정하지 않고 기존 가로구역 분석 시도

### 서버 health
- `_road_zip_path()` NameError 복구 상태 유지
- `_reference_data_readiness()` 추가
- `reference_data`, `reference_data_missing`, `analysis_reference_ready` 노출
- `_road_shape_zip_cache_dir()`에만 `@lru_cache(maxsize=1)` 유지

### 기존 판정 보존
- 한성대입구 약 305m 단일노선/중심지 확대요건 없음 -> 역세권 FAIL, 입지유형 FAIL
- 특성관리지구 공식도면 복원자료 판정 유지
- 간선가로 REVIEW/FAIL 분리 유지
- 도시계획시설 단순중첩은 자동 hard gate로 사용하지 않는 기존 R5/R6 결정 유지

## 3. 실행 검증
- `python -m py_compile app.py`: PASS
- HTML inline JavaScript `node --check`: PASS
- FastAPI TestClient `GET /`: 200
- FastAPI TestClient `GET /health`: 200
- R4 targeted regression: ALL PASS
- R5 targeted regression: ALL PASS
- R6 targeted regression: ALL PASS
- R8 targeted regression: ALL PASS
- 동적 JS mock: `basic_unit=false`일 때 `analyzeSchemeStreetBlocks()`가 도로/VWorld 함수 호출 0회로 즉시 REVIEW 반환: PASS

현재 검증 작업폴더의 `/health` 결과:
- `road_bundled_configured=true` (검증폴더에는 기존 도로 ZIP 존재)
- `street_block_basic_unit_configured=false`
- `analysis_reference_ready=false`
- 누락 기준자료를 `reference_data_missing`에 정상 노출

## 4. 원본 전체 regression_checks.py
원본 `regression_checks.py`는 코드 수정 없이 그대로 유지했다.
현재 작업폴더에는 `uq181_legal.zip`이 없으므로:
- `measurement`: PASS
- 다음 `renewal spatial`: `FileNotFoundError: uq181_legal.zip`로 중단

이는 R8 코드 회귀가 아니라 해당 작업패키지의 기준자료 누락이다. 원본 테스트를 통과시키기 위해 검사기를 수정하지 않았다.

## 5. 첨부된 R7-FIX1과의 관계
검토 시 전달된 `analysis-pipeline-r7-fix1`은 실제 코드상 가로구역 백그라운드 병렬구조를 포함한다. 사용자/협업 검토에서 최종 합의한 "preflight는 채택하되 대형 공간분석은 순차 유지"와 다르므로 최종본으로 채택하지 않았다.

R8 FINAL은 R6-FIX2의 순차 구조를 기준으로 필요한 R7 기능만 선별 병합한 최종 패치다.
