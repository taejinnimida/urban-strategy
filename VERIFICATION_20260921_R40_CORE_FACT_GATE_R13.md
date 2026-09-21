# R13 검증보고서 — CORE FACT GATE

## 검증결과

- 신규 R13 회귀검사: 31/31 PASS
- 기존 회귀검사 전체: 273/273 PASS
- 총계: 304/304 PASS
- JavaScript syntax: PASS
- `app.py` compile: PASS

## 신규 합성검사 범위

- 23개 레이어 전부 정상/빈값일 때 `planningComplete=true`
- 22개 성공 + 1개 `ERROR`, 1개 `NOT_RUN` 차단
- 정상 0건 `SUCCESS_EMPTY`와 조회오류 구분
- 오류 빈 배열을 중첩 없음으로 표시하지 않음
- 불완전 기본현황에서 판정엔진 호출 0회, 완결 후 1회
- 공식면적 누락 시 과소필지 확정값 차단
- `MIXED` 과소필지 REVIEW, `OFFICIAL`만 비율 산출
- 건축물 공간 오류 재전달 및 정상 0건 상태
- 건축HUB `SUCCESS_DATA` / `SUCCESS_EMPTY` / `ERROR` PNU 구분
- HUB 모집단 불완전 시 노후도 REVIEW
- 모집단 완전 시 하한 PASS·상한 FAIL·구간 REVIEW 유지
- 같은 구역계 정상 캐시 재사용, 다른 구역계 캐시 분리
- 재시도 가능 오류 2회 재시도, `ERROR` 비저장

## 기존 회귀검사

- R3 도시재생 통합: PASS
- R4 역세권활성화·사전협상 경합: PASS
- R5 분석 안정성: PASS
- R6 용도지역 핵심 FACT: PASS
- R7 용도지역 서버 중계: PASS
- R8 준공업 팝업: PASS
- R9 지구단위계획 CURRENT PLAN: PASS
- R10 지구단위계획 모달·간선가로 UI: PASS
- R11 공시지가·사업성 보정계수: PASS
- R12 규제 3패널·재해: PASS
- R12 재해 SHP 로더: PASS

R12 배포 ZIP에 과거 비교용 `app.before*.html` 3개가 포함되지 않아 해당 비교항목은 실행 가능한 후속 버전 핵심함수 해시검사로 대체했다. 해당 경로 외의 기존 검사는 모두 실행했다.

## 핵심 함수 보존 확인

R12 전용 회귀검사에서 다음 함수가 byte-identical임을 확인했다.

- `densityForScheme`
- `runAllSchemeChecks`
- `checkActivationFromFacts`
- `analyzeSchemeStreetBlocks`
- `analyzeRoadAccess`
- `buildSiteFactStore`

## 외부 API 검증 구분

### 로컬에서 완료

- 상태집계·게이트·캐시·재시도 합성응답 단위검사
- 건축HUB PNU별 데이터/빈값/오류 합성검사
- 원격 재해 ZIP 로더 기존 검사
- Python/JavaScript 구문검사와 기존 회귀검사

### 배포 후 런타임 확인 필요

- 실제 VWorld 23개 레이어 응답과 등록 도메인
- direct → 공식 proxy fallback 실동작
- 실제 429의 `Retry-After` 처리
- 실제 건축HUB 빈 필지와 일부 PNU 장애 응답
- Render 재시작·콜드스타트·동시접속 환경의 처리시간

검증 로그: `REGRESSION_ALL_R13.log`

