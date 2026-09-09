# v2.5.0 R6-FIX3 — Claude fix2 베이스 + R7 preflight/health 진단 선별 병합

기준본: `urban-strategy-v2_5_0-20260909-activation-streetblock-r6-fix2.zip` (Claude)
가져온 부분: GPT R7의 가로구역 preflight, `/health` 기준자료 진단
가져오지 않은 부분: R7의 가로구역 백그라운드 병렬실행

## 병합 판단 근거

GPT의 지적대로 fix2는 `runSiteReview()`가 `제도별 가로구역`을 최대 300초까지 완전히
기다린 뒤에야 연속지적·건축물 등 나머지 FACT 수집을 시작하는 구조였다. `basic_unit_seoul.zip`이
없는 현재 상태(이번 대화에서 반복 확인됨)에서는 이 대기가 실제로 수 분까지 늘어질 수 있다 —
"구역은 결정되는데 값이 안 나온다"는 증상과 직접 부합한다.

반면 R7의 해법(가로구역을 백그라운드로 던지고 다른 FACT와 동시 실행)은 채택하지 않았다.
`runAllAutoAnalyses()`에 이미 있던 기존 주석 — "대형 공간자료·외부 API는 순차 실행한다.
작은 Render 인스턴스에서 SHP 2종 동시 적재로 HTML 오류/프로세스 재시작이 나는 가능성을
낮춘다" — 이 이 저장소에서 실제로 겪었던 문제를 근거로 순차실행을 채택한 기존 설계결정임을
코드에서 확인했다. 가로구역 엔진도 `road_shp_seoul.zip` 기반 대형 SHP·도로연산 + 4개 제도
백엔드 POST를 쓰므로, 이걸 다른 대형 공간자료 조회와 동시에 돌리는 것은 같은 리스크를
다시 끌어들일 수 있다. 확정된 이득(누락 데이터일 때 즉시 REVIEW)이 preflight만으로도
얻어지는 이상, 병렬화까지는 채택하지 않는 것이 안전하다는 판단에 동의한다.

## 변경 내용

### `app.html`
1. `streetBlockRuntimePreflight()` / `markSchemeStreetBlocksUnavailable()` 신규 함수 추가(R7 원안 그대로).
2. `analyzeSchemeStreetBlocks()` 최상단에 preflight 호출 추가 —
   `/health`의 `street_block_basic_unit_configured`가 `false`로 확인되면 VWorld 다중조회·
   백엔드 4회 POST를 시작하지 않고 5초 내 즉시 전 제도 REVIEW로 표시.
3. `runSiteReview()` / `runAllAutoAnalyses()`의 실행 **순서 자체는 변경하지 않음** — 가로구역은
   여전히 다른 FACT보다 먼저 완전히 끝난 뒤 다음 단계로 넘어간다. R7의
   `streetBlockPromise`/`skipStreetBlocks` 백그라운드-조인 구조는 가져오지 않았다.
4. 두 곳의 `classify` 콜백에 "기초단위구 미설치" 사유를 인식해 `rejected`가 아닌 `partial`로
   분류하는 처리 추가 (검토 상태 문구가 "실패"가 아니라 "확인필요"로 정확히 표시되도록).
5. 가로구역 스텝 타임아웃은 기존 300000ms(5분)를 그대로 유지 — preflight가 "누락" 케이스를
   흡수하므로, 남은 300초는 "자료는 있는데 VWorld 응답이 느린" 드문 케이스에 대한
   안전판으로만 쓰인다.

### `app.py`
1. `_reference_data_readiness()` 신규 함수 추가 (R7 원안과 동일 — `stations.json`, `centers.json`,
   `station_entrances.json`, `uq181_legal.zip`, `uq120_project.zip`, `safe_medical_reference.json`,
   `biotope_seoul.zip`, `forest_classification_seoul_202608.zip`,
   `school_protection_seoul_202608.zip`, 기초단위구 설치여부).
2. `/health`에 `reference_data`, `reference_data_missing`, `analysis_reference_ready` 3개 필드 추가.
3. `_road_zip_path()`/`_road_shape_zip_cache_dir()`/`street_block_basic_unit_configured`는
   fix2 상태 그대로 유지 (R7보다 캐시 위치가 정확하다는 점은 앞선 검토에서 이미 확인됨 —
   이번에 다시 손대지 않았다).

## 검증
- `python -m py_compile app.py`: PASS
- inline JS `node --check`: PASS
- FastAPI TestClient: `GET /` → 200, `GET /health` → 200,
  `road_bundled_configured=true`(road_shp_seoul.zip 포함 시), `street_block_basic_unit_configured=false`,
  `analysis_reference_ready=false`, `reference_data_missing`에 누락 10개 항목 정확히 노출
- 기존 회귀검사 3종(base/r5/r6) 전부 재실행 → 전부 PASS
- R7의 `targeted_regression_checks_r7.py`를 참고로 실행 — 대부분 PASS(캐시 위치, 기초단위구 노출 등),
  단 `streetBlockPromise` 변수명을 찾는 "가로구역이 연속지적을 막지 않는다"류 검사 1건은 의도적으로
  실패함(=병렬실행을 채택하지 않았다는 것을 그대로 보여주는 결과이며, 버그가 아니다).

## 남은 데이터 의존성 (이전과 동일)
`reference_data_missing`에 나열되는 10개 항목은 여전히 이 배포 ZIP에 없다. 이 부분은 코드로
해결할 수 있는 범위가 아니며, 배포 서버(또는 기존 전체 저장소)에 실제 데이터 파일을 복원해야
`analysis_reference_ready=true`가 된다.
