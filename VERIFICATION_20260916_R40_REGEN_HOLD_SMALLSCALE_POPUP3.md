# R40 도시재생 검토 잠정중지 + 소정법 POPUP3 반영 검증

## 기준본
- `urban-strategy-v2.5.0-20260915-r40-FINAL-UI-RESULT-POPUP3-DOR.zip`
- 앱 버전 `v2.5.0` 유지

## 1. 도시재생 관련 지역·지구 검토 잠정 중지

### 원인 확인
기존 R40은 `analyzeUrbanRegenerationRestrictions()`에서 VWorld NED 토지이용계획정보를 실제 조회하면서도 사이트 분석 `도시계획(개발)구역` 카드에는 `도시재생사업 인허가 = 공식 공간자료 미연결`을 고정 표시했다. 반면 `publicComplexUrbanRegenerationFact()`에서는 조회 결과를 도심공공주택복합 PASS/FAIL 배제판정에 사용해 화면과 판정 경로가 불일치했다.

### 변경
- `URBAN_REGENERATION_RESTRICTION_PAUSED=true` 플래그 추가.
- 도시재생 관련 지역·지구 API 자동조회 호출 중지.
- 사이트 분석 카드의 해당 항목을 회색 `잠정 중지 / 자동검토 보류` 상태로 표시.
- 수기 `도시재생활성화계획 의제경로` 체크박스 비활성화.
- 도심공공주택복합의 도시재생 관련 배제항목은 `REVIEW`로 고정하여 사업판정을 보류.
- 자율주택 사업대상지 판정에서 도시재생활성화지역 및 이름에 `도시재생`이 포함된 기존사업을 잠정적으로 진입근거에서 제외.
- 지구단위계획·도시개발구역·공공주택지구·기타 개발사업 GIS는 정상 유지.

## 2. 소규모주택정비사업 기초검토서 POPUP3 미반영 원인 및 수정

### 원인 확인
실제 기준본 `renderSmallscaleSchemeDetailPopup()`은 `1. 공통 현황`, `2. 사업요건 검토`만 렌더링하고 종료했다. Library의 `SMALLSCALE_POPUP3_INSERTION_TEMPLATE.js` 및 후속 Rule 정리내용은 메인 `app.html`에 semantic merge되지 않은 상태였다. 따라서 계산오류가 아니라 **실제 렌더러 병합 누락**이 원인이었다.

### 변경
`renderSmallscaleSchemeDetailPopup()`에 `3. 계획기준·용적률·공공부담`을 실제 삽입하고 사업별 전용 Rule을 연결했다.

- 자율주택정비사업
  - 현 용도지역 조례용적률 및 2·3종 한시완화 표시
  - 통합심의 시 법적상한용적률 120% 조건부 특례 표시
  - 법적상한 초과분 공공환수 50%, 조건부 40% 완화 경로 표시
  - 정비기반시설 제공 특례 별도 표시
- 가로주택정비사업
  - 현 용도지역 조례용적률
  - 임대주택 10~20% 비례완화 / 20% 이상 법적상한 경로
  - 정비기반시설·공동이용시설 제공 시 120% 범위 특례
  - 모아타운은 법정사업이 아닌 관리계획 overlay로 표시
- 소규모재건축사업
  - 2종 200→250, 3종 250→300
  - 1종→2종, 2종→3종은 목표 용도지역을 사용자가 선택한 경우에만 표시
  - 운영기준 제5장 계획기준 및 국민주택규모 주택 의무
  - 조례용적률 초과분 50% 상당 공공환수
- 소규모재개발사업
  - 소규모재건축과 숫자가 같아도 별도 Rule 객체/분기로 반환
  - 종전 용도지역 조례용적률 초과분 50% 상당 공공환수
  - 단순 `공공기여 10%`로 환산하지 않음

`densityForScheme()`, 후보순위, PASS/FAIL 엔진, Fact Store, 가로구역·접도·구릉지 로직은 변경하지 않았다.

## 3. 보존 확인
함수 본문 SHA 비교 결과 다음 핵심 엔진은 기준본과 동일하다.
- `densityForScheme()` — UNCHANGED
- `runAllSchemeChecks()` — UNCHANGED
- `buildSiteFactStore()` — UNCHANGED
- `analyzeSchemeStreetBlocks()` — UNCHANGED
- `analyzeRoadAccess()` — UNCHANGED

## 4. 회귀검사
- R35: 33/33 PASS
- R36: 24/24 PASS
- R37: 33/33 PASS
- R38: 30/30 PASS
- R39: 25/25 PASS
- R40: 38/38 PASS
- R40 UI FINAL: 45/45 PASS
- 기존 도정법 POPUP3: PASS
- 신규 `R40 REGEN HOLD + SMALLSCALE POPUP3`: 27/27 PASS
- inline JavaScript syntax: PASS

## 5. 추가 파일
- `targeted_regression_checks_r40_regen_hold_smallscale_popup3.py`
- `R40_REGEN_HOLD_SMALLSCALE_POPUP3.patch`

## 6. 운영 원칙
도시재생 관련 지역·지구 자동검토를 재개할 때는 `URBAN_REGENERATION_RESTRICTION_PAUSED=false`만 단순 변경하지 말고, 공식 공간자료/인가자료의 범위와 판정대상을 먼저 확정한 뒤 전용 회귀검사를 갱신한다.
