# VERIFICATION — R40 역세권활성화·사전협상 경합전략 R4

기준 작업본: `urban-strategy-v2.5.0-20260918-r40-REGULATORY-WIP-URBAN-REGEN-INTEGRATED-R3.zip`

## 1. 수정 목적

5,000㎡ 전후 대상지에서 역세권활성화와 사전협상이 동시에 사업성 제고 대안이 될 수 있으나, 기존 화면에서는 역세권활성화가 조건미달로 약화되고 사전협상은 자료확인 필요로 남아 두 제도가 모두 후보군에서 약화되는 문제를 보완한다.

법적 원판정(PASS/FAIL/REVIEW)은 변경하지 않고, 원판정 이후 별도 전략 RESULT로 `사업성 제고제도 경합`을 표시한다.

## 2. 구현 내용

- `schemeCompetitionState` 독립 전략상태 추가
- `analyzeActivationPriorNegotiationCompetition()` 추가
- 경합 성립 조건
  - 대상지 면적 5,000㎡ 이상
  - 사전협상 대상여부가 아직 확정되지 않음
  - 역세권활성화 기본 입지·면적 검토가 살아 있음
  - 별도의 독립적인 역세권활성화 필수 FAIL이 없는 경우에만 역세권활성화를 전략후보로 유지
- 사전협상 역시 독립 필수 FAIL이 없는 경우에만 전략후보로 유지
- 화면 상단에 빨간색 `사업성 제고제도 경합` 패널 추가
  - 역세권활성화: `경합검토 · 조건부 추진후보`
  - 사전협상: `추진후보 · 조건부 추진후보`
- 기존 후보 테이블에서도 두 사업을 빨간색 조건부 후보로 노출
- 역세권활성화 카드에 `역세권형 / 간선가로형` 경로 상태 표시
- 노선형 상업지역 판정 결과를 간선가로형 UI에 연결
- 선택필지 면적을 이용해 `1필지 제외 시 5,000㎡ 미만` 가능 조합 존재 여부 계산
  - 자동 필지제외 또는 특정 필지 추천은 하지 않음
  - 사업구역 조정 가능성 FACT만 표시
- 추천/AI FACT에 전략경합 상태 전달

## 3. 보존 확인

다음 원판정/핵심 함수는 수정 전후 SHA-256 동일:

- `checkActivationFromFacts()` — `9656bb0b44a514ef...`
- `priorNegotiationSpatialFacts()` — `1595572e0678f49e...`
- `checkPriorNegotiationFromFacts()` — `fb89d91796ef82ef...`
- `densityForScheme()` — `8200438b9fa05b11...`
- `buildSiteFactStore()` — `f0223a1e8a2ea50c...`
- `analyzeSchemeStreetBlocks()` — `452e520df0492f09...`
- `analyzeRoadAccess()` — `747f5d4c0422cca8...`
- `runAllAutoAnalyses()` — `d7e776b9b6b57b7c...`

`runAllSchemeChecks()`는 기존 RULE 실행 후 전략경합 상태를 계산·표시하기 위한 최소 호출부만 추가했다.

## 4. 시나리오 검증

1. 5,093㎡ + 역세권 기본요건 살아있음 + 사전협상 대상여부 미확정
   - 경합 활성
   - 역세권활성화 전략후보 유지
   - 사전협상 전략후보 유지

2. 위 조건 + 역세권활성화에 별도 독립 필수 FAIL 존재
   - 경합관계 안내는 가능
   - 역세권활성화는 전략후보로 구제하지 않음

3. 사전협상 대상여부 확정
   - 경합 상태 종료

4. 면적 4,900㎡
   - 경합 상태 미발생

## 5. 회귀검사 결과

`regression_check_activation_prior_competition_r4.py`

**30 / 30 PASS**

포함 검증:
- R3 도시재생혁신지구 SHP 통합 유지
- 구 도시재생 GeoJSON/NED 코드 부재 유지
- 핵심 함수 해시 보존
- 원판정 결과 overwrite 금지
- 빨간색 경합 UI
- 역세권활성화 경로 UI
- 1필지 제외 시나리오
- 추천 후보 유지
- AI FACT 연계
- retry13 1회·순차 재분석 유지
- JavaScript 문법검사
- `app.py` 컴파일

## 6. 주의사항

이번 R4는 법적 PASS/FAIL을 재정의하지 않는다. `경합검토/조건부 추진후보`는 사업전략 RESULT이며, 사전협상 대상지 확정 또는 역세권활성화 독립 배제사유가 확인되면 기존 RULE 결과를 우선한다.

또한 1필지 제외 시나리오는 구역계 조정 가능성을 알리는 정보일 뿐, 특정 필지 제외를 자동 권고하거나 법적 적용을 보장하지 않는다.
