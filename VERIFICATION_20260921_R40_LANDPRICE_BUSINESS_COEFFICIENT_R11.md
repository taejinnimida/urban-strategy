# VERIFICATION — R40 LANDPRICE / BUSINESS COEFFICIENT R11

검증일: 2026-09-21

## 1. 변경범위

- `app.html`: 토지정보 평균공시지가 FACT UI/함수, 사업성 보정계수 RULE 및 UI, 재건축 α·β 산식, 향후 허용용적률 적용 순수함수
- `app.py`: 변경 없음
- 기존 데이터 파일: 변경 없음

## 2. R11 전용 회귀검사

`regression_check_land_price_business_coefficient_r11.py`

결과: **33/33 PASS**

주요 검사항목:

- 토지정보에 사업구역 평균공시지가/기준연도/커버리지 노출
- 지목 `대` 모집단 유지
- 면적가중평균 유지
- 경계부분 중첩필지 비례면적 유지
- 재개발 1.00~2.00 규칙
- 재건축 α/β 공식 구현
- 소정법 1.00~1.50 및 직접 적용경로 분리
- 소규모재건축 임의산식 금지
- 향후 허용용적률 적용 산식 준비
- 현재 밀도계산 미연결
- R10 핵심함수 해시 보존
- browser JavaScript syntax PASS
- app.py compile PASS

## 3. 수식 단위검증

- α 10,000㎡ → 0.20 PASS
- α 16,813.7㎡ → 0.14 PASS
- α 20,000㎡ → 적용대상 아님(0) PASS
- β: 가용용적률 261.92%, 대지 16,813.7㎡, 현황 344세대 → β 미적용 PASS
- β: 세대당 공급가능면적 80㎡ 미만 → 0.20 PASS
- β: 세대당 공급가능면적 95㎡ → 0.15 PASS
- 가용용적률 미연계 → REVIEW + 필요 용적률 역산 PASS
- 보정 허용용적률: 기준 210%, 허용 230%, 계수 1.5 → 240% PASS
- 소수점 셋째자리 올림 및 계수 상·하한 clamp PASS

## 4. 기존 회귀검사

- R4 경합로직: **30/30 PASS**
- R5 안정화: **19/19 PASS**
- R6 용도지역 FACT: **19/19 PASS**
- R7 용도지역 서버조회: **20/20 PASS**
- R8 준공업 팝업: **44/44 PASS**
- R9 지구단위 CURRENT PLAN: 작업폴더의 역사 fixture를 사용한 별도 검증에서 **26/26 PASS**
- R10 지구단위 modal/간선도로 UI: **22/22 PASS**
- R3 도시재생 통합: 기능검사 **26/27 PASS**. 실패 1건은 R3 당시 `runAllSchemeChecks()`의 byte hash와 현재 코드를 비교하는 역사검사이며, R4에서 해당 함수가 의도적으로 변경된 이후의 현행 해시 `ea1389...`는 R4/R6/R7/R8/R10/R11 검사에서 모두 보존됨. R11 기능 회귀로 판단하지 않음.

## 5. 핵심함수 R10 해시 보존

- `buildSiteFactStore`: `f0223a1e8a2ea50c9a107a838dcd80332bc383fd90c3f5897f98023f1692a2db`
- `densityForScheme`: `8200438b9fa05b114c025d275167eecba7f447fd7c9812e868562b662245f7a8`
- `runAllSchemeChecks`: `ea1389e3a751711a373b91db19c856521097697dc9e83eb715e6ab8dfbd8e1db`
- `checkActivationFromFacts`: `9656bb0b44a514efc40679cf536c34354695b7912056140227e86853581941a2`
- `analyzeSchemeStreetBlocks`: `452e520df0492f09772bfab97d0c9bf84620954d787f92472060e3853e14af23`
- `analyzeRoadAccess`: `747f5d4c0422cca81632ad9fe4f3d57d2e16c219f39f5506bfdf9b2ceaff646c`
- `runAllAutoAnalyses`: `d7e776b9b6b57b7ce6186186c62ab9b357d9734d5ea93af00cce407e462979ee`

## 6. 범위 제한 확인

이번 R11에서는 다음을 하지 않았다.

- 사업별 PASS/FAIL 변경 없음
- `densityForScheme()` 변경 없음
- 사업팝업 밀도계산 연결 없음
- 상한/법적상한용적률 자동 적용 없음
- 공공기여 산식 변경 없음
- 지구단위계획 코드 변경 없음
- API/backend 변경 없음


## 7. 최종 ZIP fresh-extract 검증

최종 패키지를 별도 폴더에 새로 압축해제한 뒤 다시 검증했다.

- R11: **33/33 PASS**
- R4: **30/30 PASS**
- R5: **19/19 PASS**
- R6: **19/19 PASS**
- R7: **20/20 PASS** (역사 fixture는 검증 시에만 임시 사용, ZIP 미포함)
- R8: **44/44 PASS**
- R9: **26/26 PASS** (역사 fixture는 검증 시에만 임시 사용, ZIP 미포함)
- R10: **22/22 PASS**
- SHA256 패키지 내부 파일 검증: PASS

검증 중 VWorld `LT_C_DAMDAN` 외부 호출에서 HTTP 502 경고 1건이 발생했으나, R8 설계대로 산업단지 외부자료 실패를 REVIEW로 유지하는 경로이며 회귀검사 44/44는 통과했다.
