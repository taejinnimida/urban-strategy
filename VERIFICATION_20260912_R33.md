# VERIFICATION 20260912 R33

## 범위
- R32 기준의 FACT 접근경로 정상화 1차 구조개편.
- `site.heritage`, `site.hill`, `site.conservation` 공통 FACT 편입.
- 안심주택 학교절대보호구역의 사업판정 silent fallback 제거.
- 역 관련 공식 사업판정 경로를 `site.station` snapshot 중심으로 통일.
- 준공업지역 정비·개발 패널을 도심정비와 특례개발 사이로 이동.
- `data_status/legal_status/requirement_type` 전면 상태모델 개편은 이번 R33에서 보류(별도 단계).

## 감사분류
| 데이터원 | R32 문제 | R33 처리 |
|---|---|---|
| 문화재 | `longterm`이 `planningAnalysis.heritage` 직접참조 | `site.heritage` 독립 FACT 경로 |
| 구릉지 | `longterm`이 `hillAnalysis` 직접참조 | `site.hill` 독립 FACT 경로 |
| 역 | 복수 스킴이 `stationAnalysis` 직접/재계산 | `site.station` snapshot/accessor 중심 |
| 학교 절대보호구역 | `safeHousingSpatialFacts`에서 store 실패 시 직접재계산 | store 미확보 시 REVIEW/NO_DATA 성격 유지, 재계산 금지 |
| 상생주택 보전환경 | `sharedHousingSpatialFacts`가 `sharedConservationAnalysis` 직접참조 | `site.conservation` 공통 FACT 경로 |
| UI preview fallback | 다수 존재 | 이번 R33 판정엔진 범위 밖, 유지 |

## 정적 검증
- PASS — SCHEME_MODULES 16개 등록 유지
- PASS — site.heritage 독립 FACT
- PASS — site.hill 독립 FACT
- PASS — site.conservation 공통 FACT
- PASS — 안심주택 학교절대보호구역 store-only
- PASS — 장기전세 문화재 store 경로
- PASS — 장기전세 구릉지 store 경로
- PASS — 역 snapshot safe350 정보 포함
- PASS — 역세권활성화 store 역 후보
- PASS — 성장잠재권 store 역 후보
- PASS — 장기전세 store 역 후보
- PASS — 공공복합 store 역 후보
- PASS — 도심복합 store 역 후보
- PASS — 소규모재개발 store 역 후보
- PASS — 상생주택 conservation 전역 직접참조 제거
- PASS — 주요 collectFacts 본문 전역 analysis 직접참조 없음 — {}
- PASS — 준공업 패널 특례개발 앞
- PASS — 동의율 전제조건 문구 유지
- PASS — 계획변수 계획반영필요 문구 유지
- PASS — 구 2030 준공업 REVIEW 문구 없음
- PASS — JavaScript syntax node --check

**결과: 21/21 PASS**

## 주의
- 이 검증은 코드구조/정적 회귀검사와 JavaScript 문법검사다. 실제 서울 API 호출·브라우저 실사이트 분석은 배포환경에서 별도 확인이 필요하다.
- R32의 법정 숫자기준은 이번 구조개편에서 의도적으로 변경하지 않았다.