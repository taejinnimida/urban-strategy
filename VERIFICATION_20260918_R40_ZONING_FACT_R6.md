# VERIFICATION — R6 용도지역 핵심 FACT 검증

## 문제
- 같은 대상지에서 VWorld 도시계획 GIS 호출은 완료되었으나 LT_C_UQ111 용도지역 도형이 0건으로 반환됨.
- 기존 코드는 error_count=0이면 도시계획 GIS 전체를 fulfilled(초록색)로 표시하고, zoningSpatialEvidenceFacts도 loaded=true이면 CONFIRMED로 취급할 수 있었음.
- 그 결과 용도지역 미확보로 사업방식은 REVIEW가 되면서도 진행현황은 성공처럼 표시되는 모순이 발생함.

## 수정
1. `용도지역 핵심 FACT` 단계를 도시계획 GIS 일반 레이어와 분리.
2. 1차 도시계획 조회에서 용도지역이 0건이면 LT_C_UQ111만 단독 재확인.
3. 단독 재확인도 실패/0건이면 rejected로 유지하고, 마지막 `분석실패 현황 재분석`에서 해당 UQ111 단계만 1회 재시도.
4. 용도지역 rows=0 또는 primary 미확보는 `known=false / REVIEW`; CONFIRMED 금지.
5. 용도지역 카드에 `핵심 Fact 미확보 · 사업판정 보류` 표시.
6. 사업판정 진행카드도 용도지역 핵심 Fact가 없으면 초록색이 아니라 partial(노랑) 및 `사업판정 보류` 표시.
7. 사업방식 상단 요약도 `현재 추진가능 없음` 대신 `핵심 FACT 미확보: 용도지역 · 사업방식 판정 보류`로 표시.

## 보존
- densityForScheme 동일
- checkActivationFromFacts 동일
- checkPriorNegotiationFromFacts 동일
- analyzeSchemeStreetBlocks 동일
- analyzeRoadAccess 동일
- runAllSchemeChecks 동일
- R4 역세권활성화↔사전협상 경합 유지
- R5 429/caching/run-id 안정화 유지

## 검사
- R6 targeted regression: 19/19 PASS
- R5 stability regression: 19/19 PASS
- R4 competition regression: 30/30 PASS
- Browser JS syntax: PASS
- app.py compile: PASS

## 기대 UI
- 정상: `용도지역 · 제3종일반주거 · 1개 구분` 등 초록색
- 미확보: `용도지역 · 핵심 Fact 미확보` 빨간색
- 사업판정: `핵심 Fact 미확보(용도지역) · 사업판정 보류` 노란색
- 상단 사업방식 요약: `핵심 FACT 미확보: 용도지역 · 사업방식 판정은 보류되며 용도지역 재확인이 필요합니다.`
