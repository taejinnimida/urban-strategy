# R40 R9 검증보고서 — 지구단위계획 CURRENT PLAN 연계

## 1. 기준본
- 직전 완료 결과물: `urban-strategy-v2.5.0-20260919-r40-REGULATORY-WIP-SEMIINDUSTRIAL-POPUP-R8.zip`
- 앱 버전: **v2.5.0 유지**
- 이번 작업은 지구단위계획 CURRENT PLAN 정보 추가이며 기존 사업 PASS/FAIL/REVIEW 규칙은 변경하지 않음.

## 2. 작업 중 발견·수정한 결함
초기 R9 시도에는 프론트가 `/api/reference/district-unit-plan-current`를 호출하지만 서버 endpoint가 존재하지 않는 결함이 있었다. 또한 설명상 서울시 UQ161/UQ165 공식 공간자료를 사용한다고 했으나 실제 구현은 VWorld 호출이어서 설명과 코드가 불일치했다. 초기 R9 작업본은 폐기하고 R8에서 재작업했다.

## 3. 실제 구현
### 공간 FACT
- 기존 공식 공공공간정보 접근방식인 VWorld `LT_C_UPISUQ161` 지구단위계획구역 중첩을 재사용.
- 지구단위계획은 `CURRENT PLAN` 정보로만 저장/표시하고 사업결과 status를 변경하지 않음.
- `LT_C_UPISUQ165`는 VWorld 공식 제공여부를 검증하지 못했으므로 호출하지 않음.
- 서울 열린데이터광장의 UQ165 특별계획구역 공간자료 존재는 공식 링크로 안내하되 자동 공간판정은 후속과제로 유지.

### 공식 참조정보
- 신규 endpoint: `POST /api/reference/district-unit-plan-current`
- 서울 열린데이터광장 서비스:
  - `upisCUq161` : UQ161 도형 관리코드/고시관리코드 확인
  - `upisDistUnitPlan` : 지구단위계획 조서 참조
- 연결 우선순위:
  1. `FIG_RPT_MNG_CD`
  2. `DCSN_ANCMNT_MNG_CD`
  3. `STUT_FIG_MNG_NO`
  4. `OBJT_ID`
  5. 유일한 `LBL_NM` 정확일치
- 부분문자열·유사명칭 자동추정은 금지.
- 지구단위계획 조서의 결정고시관리코드는 표시하되 결정고시 본문/최근 변경고시 자동판정은 이번 범위에서 확정하지 않음.

### UI
- 사이트분석에 `지구단위계획 / 현재 계획기준` 카드와 미니맵 추가.
- 표시: 구역명, 중첩률, 서울시 공식조서 연결상태, 관리코드, 결정도서/공식정보 링크.
- 획지·공동개발·용도·용적률·높이·건축선·벽면선은 공식 외부피드 자동연계 미확정으로 문서검토 유지.
- 모든 사업 팝업 하단에 현행 지구단위계획 관계를 REVIEW 정보로 추가하지만 사업 status는 변경하지 않음.
- AI 입력에는 CURRENT PLAN과 REVIEW ITEM만 추가하며 변경·완화·의제 가능성을 자동단정하지 않음.

## 4. 운영조건
- 서울 열린데이터 참조조회에는 기존 서버 환경변수 `SEOUL_OPEN_DATA_KEY` 계열이 필요함.
- 키가 없거나 API가 실패해도 UQ161 공간 FACT와 기존 사업판정은 유지하고 참조정보만 `NO_KEY/ERROR`로 표시.
- 현재 검증환경에는 서울 열린데이터 API 키가 없어 실제 live 응답은 미검증. 관리코드 연결 알고리즘은 독립 단위시험으로 검증함.

## 5. 회귀검증
- R3 도시재생 통합: **27/27 PASS**
- R4 역세권활성화-사전협상 경합: **30/30 PASS**
- R5 반복분석 안정화: **19/19 PASS**
- R6 용도지역 핵심 FACT: **19/19 PASS**
- R7 용도지역 서버조회: **20/20 PASS**
- R8 준공업 팝업: **44/44 PASS**
- R9 지구단위 CURRENT PLAN: **26/26 PASS**
- `app.py` compile PASS
- browser JavaScript syntax PASS

## 6. R8 대비 보존 확인
다음 핵심 함수는 R8과 byte-identical:
- `buildSiteFactStore()`
- `densityForScheme()`
- `runAllSchemeChecks()`
- `analyzeSchemeStreetBlocks()`
- `analyzeRoadAccess()`
- `runAllAutoAnalyses()`

## 7. 후속과제
1. UQ165 공식 SHP 또는 공식 외부 공간 API를 실제 배포데이터로 연결.
2. 획지·공동개발·건축선·용적률·높이 등 세부결정 객체의 공식 외부 제공방식 별도 검증.
3. `upisAnnouncement` 최근 변경고시 자동연계는 라이선스·조회비용·정확한 관리코드 연결을 재검토한 뒤 별도 패치.
