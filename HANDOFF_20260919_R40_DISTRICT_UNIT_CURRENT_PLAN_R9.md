# R40 R9 인수인계 — 지구단위계획 CURRENT PLAN

## 기준
직전 완료 R8을 기준으로 최소변경. 초기 실패 R9 시도는 사용하지 않는다.

## 핵심원칙
- 지구단위계획 = CURRENT PLAN / BASELINE.
- 지구단위계획과 사업계획 차이만으로 PASS/FAIL/REVIEW를 변경하지 않는다.
- 차이는 변경·완화·의제·추가검토 REVIEW ITEM으로만 표시.
- 최종 법률·계획 판단은 전문가 영역.

## 신규 서버 API
`POST /api/reference/district-unit-plan-current`
입력: UQ161 중첩도형 최소 식별정보 배열 `hits`
출력: 서울시 공식 조서 연결상태, 관리코드, 지역/위치/면적, 결정고시관리코드.

## 데이터
- 공간: 기존 VWorld `LT_C_UPISUQ161`.
- 공식 참조: 서울 열린데이터 `upisCUq161`, `upisDistUnitPlan`.
- UQ165 특별계획구역: 공식 데이터 존재 확인, 자동 공간연계는 아직 하지 않음.

## 주의
- `SEOUL_OPEN_DATA_KEY` 미설정 시 공식조서 참조만 비활성. 기존 공간분석/사업판정은 영향 없음.
- `LT_C_UPISUQ165`를 임의 VWorld 레이어로 추가하지 말 것.
- 유사명칭 fuzzy match 금지. 관리코드 또는 유일한 정확 라벨만 사용.
