# R28 검증보고서 — 가로구역 브라우저 멈춤 방지 / 프론트 후처리 경량화

기준본: `urban-strategy-v2.5.0-20260911-candidate-roadfact-r27-PATCH.zip`

## 1. 증상

배포환경에서 약 5,758㎡ 대상지 검토 중 `제도별 가로구역 분석 중` 단계가 3분 이상 지속되고 Chrome이 `응답 없는 페이지` 경고를 표시했다.

단순한 서버 fetch 대기는 브라우저 이벤트 루프를 점유하지 않으므로, 이 증상은 가로구역 서버 응답 전후의 프론트 동기 공간후처리(Turf union/difference/지도 반영)가 메인 스레드를 장시간 점유하는 경우를 우선 의심할 수 있다.

## 2. 확인된 프론트 병목

R27 `schemeBlockComponentsAfterRemoval()`은 320/700m 가로구역 응답을 받은 뒤 제도별 `existingRoadCutters + planningRoadCutters + exclusionCutters`를 전부 `safeUnionPolygons()`으로 합친 다음 raw block에서 차감했다.

가로구역 도로 FACT 자체는 750m 범위를 수집하므로, 대상지와 실제로 교차하지 않는 원거리 cutter까지 Turf polygon union 대상이 된다. 도심의 복잡한 버퍼도로 polygon을 연쇄 union하면 결과도형 정점 수가 증가하면서 브라우저 메인 스레드가 오래 점유될 수 있다.

또한 `schemeExistingRoadInputs()`은 `thresholdSurfaces.length===0`인 경우 unmatched TL_SPRD_MANAGE 중심선 fallback buffer를 첫 루프와 두 번째 루프에서 중복 생성할 수 있었다.

## 3. R28 수정

### 3.1 750m 전체 cutter 선-union 제거

기존:

`all cutters -> turf.union 전체 결합 -> raw block difference`

R28:

`cutter bbox 1회 산정 -> raw block bbox와 겹치는 cutter만 선별 -> raw block에서 순차 difference`

도형의 bounding box가 서로 겹치지 않으면 실제 geometry도 교차할 수 없으므로 원거리 cutter 제거는 판정/집합 결과에 영향을 주지 않는다. 또한 집합론상 `A - union(B_i)`와 순차적인 `(...((A-B1)-B2)...-Bn)`은 동일한 차집합을 표현한다.

목적은 법정 Rule이나 가로구역 경계를 변경하는 것이 아니라, 불필요한 Turf union 중간도형 생성을 제거하여 브라우저 메모리와 CPU를 줄이는 것이다.

### 3.2 도로 fallback cutter 중복 제거

- 실제 surface가 있으면 해당 surface와 매칭된 중심선은 종전처럼 skip.
- surface가 0건이면 TL_SPRD_MANAGE 중심선을 정확히 1회만 ROAD_BT 폭원 buffer.
- 종전의 `thresholdSurfaces.length===0` 두 번째 전체 loop 제거.

동일 도로 buffer가 중복 포함되던 경우만 제거하므로 공간 Rule은 변하지 않는다.

### 3.3 브라우저 이벤트 루프 양보

batch 결과의 각 제도별 프론트 후처리 사이에 `setTimeout(..., 0)` yield를 추가했다.

진행문구를 `가로구역 · [제도명] 도형 후처리 n/N`으로 갱신한 뒤 브라우저가 paint/input을 처리할 기회를 주고 다음 제도 후처리를 실행한다.

### 3.4 성능 로그 보강

각 제도 `applySchemeStreetBlockData()` 프론트 후처리 시간을 `frontend_postprocess_ms`로 metadata 및 Console에 기록한다.

기존 R22/R23 서버 측 `rule_apply_ms`, `component_pass_ms`, neighbor graph 계측과 함께 서버/브라우저 병목을 분리해 확인할 수 있다.

## 4. 보존사항

- R20 가로구역 면적 선필터 유지
- R21 4m/6m 주택접도율 유지
- R22 prepared geometry 유지
- R23 lazy neighbor graph 유지
- R24 지번 입력/지도 클릭 구역계 확정 유지
- R25 역세권활성화 가로구역/접도 정합성 유지
- R26 접도 Fact 사업별 연결 + 최소면적 10% 추천필터 유지
- R27 대안추천 상태분리 + 역세권 특례 도로 Fact 정합화 유지
- 내부 앱 버전 `v2.5.0` 유지

## 5. 검증

- `python -m py_compile app.py`: PASS
- Inline JavaScript `node --check`: PASS
- R15: PASS
- R17: 15/15 PASS
- R18: 15/15 PASS
- R19: 8/8 PASS
- R20: 10/10 PASS
- R21: 16/16 PASS
- R22: 13/13 PASS
- R23: 26/26 PASS
- R24: 18/18 PASS
- R25: 15/15 PASS
- R26: 37/37 PASS
- R27: 25/25 PASS
- R28 신규: 14/14 PASS

R28 신규검사는 가로구역 프론트의 global cutter union 제거, bbox 지역필터, local sequential difference, event-loop yield, frontend postprocess 성능계측, 도로 fallback 중복제거 및 R20/R23/R24/R26/R27 핵심 기능 보존을 확인한다.

## 6. 배포 후 확인 포인트

동일 문제 대상지를 다시 실행하여 Console의 다음 값을 확인한다.

1. `batch 320m` / 필요 시 `batch 700m`: 서버 왕복시간
2. 각 제도 `Rule ...ms`: 서버 Rule 연산시간
3. 각 제도 `frontend ...ms`: 브라우저 Turf 후처리시간
4. Chrome `응답 없는 페이지` 재현 여부

R28 후에도 한 제도의 `frontend_postprocess_ms`가 수초~수십초 이상이면 다음 단계는 해당 후처리를 Shapely 백엔드로 이전하는 방식이 적절하다. 서버시간 자체가 길면 R22/R23 계측값을 기준으로 별도 백엔드 최적화를 진행한다.
