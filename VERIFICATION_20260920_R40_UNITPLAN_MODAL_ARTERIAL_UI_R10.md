# VERIFICATION — R10 지구단위계획 상세 UI / 역세권활성화 간선도로 UI

## 1. 기준본
- 직전 결과물: `urban-strategy-v2.5.0-20260919-r40-REGULATORY-WIP-DISTRICT-UNIT-CURRENT-PLAN-R9.zip`
- 내부 앱 버전: **v2.5.0 유지**
- 수정범위: **UI 연결만**. 서버 API·사업 RULE·PASS/FAIL/REVIEW 산식은 변경하지 않음.

## 2. 수정사항
### 2.1 지구단위계획 전용 상세검토창
- 사이트분석 `지구단위계획 / 현재 계획기준` 카드 우측에 `상세검토` 버튼 추가.
- 공용 modal `districtUnitPlanReviewModal` 추가.
- 표시내용:
  1. UQ161 공간중첩 현황
  2. 공식 참조조서/결정고시 관리코드
  3. 아직 외부 공식데이터가 연결되지 않은 세부 결정사항
  4. 검토사업과 CURRENT PLAN의 관계
  5. REVIEW ITEM
- 사업방식 팝업 하단의 지구단위계획 REVIEW에서도 동일 modal을 열 수 있게 연결.
- 현행 지구단위계획 정보는 기존 사업 RESULT를 변경하지 않음.

### 2.2 역세권 특례제도 간선도로 UI
- 기존에는 역세권활성화를 `접도` 표에만 표시하고 `간선도로` 표에서는 의도적으로 제외했음.
- 사용자 관점에서 누락처럼 보이므로 `역세권활성화(간선가로형)` 행을 간선도로 표에 추가.
- 이 행은 4m/8m 접도 또는 20m/35m 폭원기준을 재사용하지 않음.
- 기존 `activationArterialAnalysis.routeStatus/path`와 노선형 상업지역 참조도형·전용 가로구역 관계만 표시.
- 기존 역세권활성화 Rule과 결과는 변경하지 않음.

## 3. 회귀검증
- R10 신규 UI 회귀: **22/22 PASS**
- R4 경합로직: **30/30 PASS**
- R5 안정화: **19/19 PASS**
- R6 용도지역 FACT: **19/19 PASS**
- R7 용도지역 서버조회: **20/20 PASS**
- R8 준공업 팝업: **44/44 PASS**
- R9 지구단위 CURRENT PLAN: **26/26 PASS**
- `app.py` compile: PASS
- 브라우저 inline JavaScript `node --check`: PASS

## 4. R9 핵심함수 보존
- `buildSiteFactStore`: UNCHANGED · f0223a1e8a2ea50c
- `densityForScheme`: UNCHANGED · 8200438b9fa05b11
- `runAllSchemeChecks`: UNCHANGED · ea1389e3a751711a
- `checkActivationFromFacts`: UNCHANGED · 9656bb0b44a514ef
- `activationArterialRouteDecision`: UNCHANGED · cbb1a5554cd30f9d
- `schemeRoadEvidenceFacts`: UNCHANGED · 484e33b35204b251
- `analyzeSchemeStreetBlocks`: UNCHANGED · 452e520df0492f09
- `analyzeRoadAccess`: UNCHANGED · 747f5d4c0422cca8
- `runAllAutoAnalyses`: UNCHANGED · d7e776b9b6b57b7c

## 5. 서버 변경
- `app.py`: **변경 없음**
- 신규 API 없음
- 서울시 CURRENT PLAN 조회방식 변경 없음

## 6. 운영상 확인사항
- 지구단위계획 modal의 세부 결정사항은 현재 `자료 미연계` 상태를 그대로 보여주며 임의값을 생성하지 않음.
- 특별계획구역 UQ165 자동 공간판정은 R9와 동일하게 별도 후속과제.
- 역세권활성화 간선도로 표의 새 행은 **UI 요약행**일 뿐 새로운 법정 판정 Rule이 아님.
