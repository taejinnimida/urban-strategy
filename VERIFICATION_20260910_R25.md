# R25 검증보고서 — 역세권활성화 판정 정합성 보정

기준본: `urban-strategy-v2.5.0-20260910-parcel-find-ui-r24-PATCH.zip`

## 1. 수정 목적

역세권활성화사업 기초검토서의 세부 판정에서 다음 두 항목을 정합화했다.

1. `가로구역 포함`의 1/2 기준을 사업대상지/가로구역 점유율이 아니라 **승강장 250/350m 역세권 범위가 해당 가로구역에 걸치는 비율**로 산정한다.
2. `도로`는 기존 도로·접도 엔진이 이미 계산한 **4m 이상 접도도로 수 + 8m 이상 접도도로 존재 여부**를 Fact Store에 직접 연결하여 PASS/FAIL을 판정한다.

## 2. FACT → RULE → RESULT

### 2.1 가로구역

- FACT: 제도별 가로구역 도형 + 판정역별 250/350m 버퍼
- CALC: `intersection(station_buffer, street_block) / street_block_area × 100`
- RULE: 역세권 범위가 가로구역의 1/2 이상 걸치면 일반경로. 1/2 미만은 폭 20m 이상 간선가로 접면 또는 구역 정형화 필요성이 인정되는 경우 위원회 심의를 통해 사업대상지로 볼 수 있음.
- RESULT:
  - 50% 이상: `PASS` 표시(일반경로)
  - 0~50% 미만: `REVIEW` 표시(위원회 심의 후 결정)
  - 이 행은 `required=false` 유지. **전체 사업가능성의 하드게이트/필수변수로 사용하지 않음.**

### 2.2 도로

- FACT: `analysisState.road_network`의 `road4Faces`, `road8Faces`, `has8`, `maxWidth` 등 도로 접도엔진 결과
- RULE: 원활한 차량 진출입이 가능한 도로로서 2면 이상이 폭 4m 이상 도로에 접하고 최소 한 면은 폭 8m 이상 도로에 접할 것
- RESULT:
  - `road4Faces >= 2 && road8Faces >= 1` → PASS
  - 그 외 수치가 확보된 경우 → FAIL
  - 수치 자체가 미확보된 경우에만 REVIEW
- 도로 Fact의 `ESTIMATE` 품질을 이유로 PASS를 REVIEW로 강등하지 않음. 지적경계·도로구역·현장 진출입 상세는 후속 단서로 유지.

## 3. 코드 보정

- `activationStationCandidates()`
  - 기존 `schemeStreetBlockRelation('activation')` 제거
  - 각 역의 적용반경별 `stationBlockRelation(st, threshold, 'activation')` 사용
  - 동일 PASS 역이 여러 개인 경우 50% 이상 일반경로를 우선한 뒤 거리순 정렬
- `activationStationCriterion()`
  - 1/2 미만을 PASS+conditional이 아니라 REVIEW(위원회 심의 후 결정)로 변경
  - 문구를 `선정사업지/가로구역`에서 `역세권 범위/가로구역`으로 정정
- `commonSchemeData()` / `compactRoadFacts()`
  - 숨은 DOM 입력값보다 실제 `analysisState.road_network` 값을 우선 사용
  - DOM은 수기/회귀 호환 fallback으로만 유지
- `checkActivationFromFacts()`
  - 4m 이상 접도면 수와 8m 이상 접도면 수로 직접 이진 판정
  - ESTIMATE라는 이유만으로 조건부/확인필요로 강등하던 처리 제거
  - 차량 진출입 상세계획은 후속 설계·인허가 확인사항으로 분리

## 4. 회귀검사

- Python compile: PASS
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
- R25 신규: 15/15 PASS

## 5. 보존사항

R20 면적 선필터, R21 주택접도율 4m/6m 판정, R22 prepared geometry, R23 neighbor graph, R24 지번 찾기/지도 클릭 UI를 변경하지 않았다. 내부 앱 버전은 v2.5.0을 유지한다.
