# R40 분석 파이프라인 안정화 R5 인수인계

기준 작업본: `urban-strategy-v2.5.0-20260918-r40-REGULATORY-WIP-ACTIVATION-PRIOR-COMPETITION-R4.zip`

## 목적
같은 대상지를 연속 검토할 때 HTTP 429/HTML 오류응답이 누적되면서 직전 정상 FACT가 0건/오류로 덮이는 현상을 방지한다.

## 확인된 재현 증상
- 다수 모듈에서 `HTTP 429` 발생
- HTML 오류페이지가 JSON 파서로 들어가 `Unexpected token '<'` 발생
- 직전 실행에서 정상인 건축HUB/도로/규제 FACT가 재실행에서 0건 또는 오류로 약화
- 기존 rejected-only 재분석이 429까지 다시 호출할 수 있었음

## R5 변경
1. 동일 구역계 단계별 FACT 캐시
   - fulfilled: 10분 재사용
   - partial: 1분 재사용
   - 구역계 변경/재확정 시 즉시 폐기
2. HTTP 429 분리
   - `RATE_LIMIT` 상태
   - Retry-After 우선, 없으면 30초 cooldown
   - rejected-only 마지막 재분석에서 429 제외
3. API 호출량 완화
   - same-origin `/api/` 시작 간격 300ms
   - 프런트 `mapLimit()` 동시 fan-out 최대 2
   - 토지대장/건축물 공간 상위단계 순차 실행
   - 백엔드 건축HUB·공시지가·관련 외부조회 worker 최대 2
4. 응답형식 방어
   - 주요 same-origin API를 `fetchBackendJson()`으로 통일
   - HTML/non-JSON 응답을 명시적 오류로 변환
5. 이전 실행 무효화
   - run-id 증가
   - 새 검토 시작 시 buildingHub/planning/streetBlock request token 무효화

## 보존
- 사업별 PASS/FAIL/REVIEW RULE
- 역세권활성화 ↔ 사전협상 경합 전략 RESULT
- 김포공항 도시재생혁신지구 SHP
- 13번 rejected-only 1회 순차 재분석 원칙
- densityForScheme/buildSiteFactStore/사업별 독립 판정 구조

## 배포 후 확인
동일 대상지를 2회 연속 검토한다.
- 두 번째 실행에서 정상 단계에 `직전 성공 Fact 재사용` 표시가 나와야 함
- 첫 실행의 정상 건축HUB/도로 FACT가 두 번째 429로 0건 처리되면 안 됨
- 429 단계는 `일시 호출제한(429)`으로 표시되고 15초 재분석 대상에서 제외되어야 함
- 새 구역계를 확정하면 이전 대상지 FACT가 재사용되면 안 됨
