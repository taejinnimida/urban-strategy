# R40 Analysis Stability R5 검증

## 수정 범위
네트워크/분석 실행 수명관리만 수정. 법적 판정 RULE 변경 없음.

## 핵심 검증
- 기존 R4 회귀검사: **30/30 PASS**
- 도시재생혁신지구 통합 회귀검사: **27/27 PASS**
- R5 분석안정화 회귀검사: **19/19 PASS**
- Browser inline JavaScript syntax: PASS
- `app.py` compile: PASS

## R5 테스트 항목
- 동일 대상지 step cache 존재
- fulfilled 10분 / partial 1분 TTL
- 구역계 변경 시 cache reset
- 같은 대상지 두 번째 safeAnalysisStep에서 함수 재호출 없이 cached result 반환
- geometry signature 변경 시 cache 미사용
- HTTP 429 → `RATE_LIMIT` 분류
- 반환 value 내부 429 오류도 RATE_LIMIT 감지
- 429는 retry13 제외
- retry13은 여전히 1회 순차 재실행
- `/api/` 요청 시작 간격 300ms
- HTML/non-JSON 응답 정상 오류변환
- mapLimit 병렬도 최대 2
- 토지대장/건축물 공간 순차실행
- 건축HUB rate-limit 시 남은 batch 중단
- 백엔드 외부자료 worker 최대 2

## 예상 동작
첫 번째 검토가 정상 완료된 뒤 같은 대상지를 다시 검토하면, 성공/부분확보 단계는 캐시를 사용하고 실패 단계만 다시 조회한다. 따라서 일시적인 429가 발생해도 직전 정상 FACT를 0건이나 NONE으로 덮지 않는다.

## 한계
- 캐시는 브라우저 메모리 기반이라 페이지 새로고침 후에는 초기화된다.
- 최초 검토부터 외부기관이 지속적으로 429/502를 반환하면 해당 FACT는 REVIEW/미확인으로 남는다.
- 본 수정은 외부기관 장애를 숨기지 않고 호출폭주와 정상 FACT 소실만 방지한다.
