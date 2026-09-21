# R14 검증기록 — 사이트분석 UI·규제자료 안정화

## 전용 회귀검사

`regression_check_site_analysis_regulatory_r14.py`

- 화면용 4칸 CSS 존재
- 2칸 wide 카드 적용
- 역세권·중심지·간선도로 순서
- 규제 3종 진행상태 등록
- 도시계획 레이어군별 진행상태 분리
- NED 성공·실패 PNU 반환
- 실패 PNU 전용 재조회
- 성공 재해자료 보존
- 합성 NED 부분실패 주입 시 성공 PNU와 양성 FACT 보존

결과: `17/17 PASS`

## 전체 회귀검사

- 회귀검사 파일 13개 모두 통과
- R13 Core Fact Gate: `31/31 PASS`
- R12 규제 3패널·재해: `47/47 PASS`
- R11 공시지가·사업성 보정계수: `33/33 PASS`
- R10 지구단위·간선도로 UI: `22/22 PASS`
- R8 준공업 팝업: `44/44 PASS`
- 용도지역 핵심 FACT: `19/19 PASS`
- 용도지역 서버 중계: `14/14 PASS`
- Python compile 및 브라우저 JavaScript syntax 통과

## 판정

- 기존 사업판정 핵심함수 해시 회귀 통과
- R13 기준 기능을 보존한 상태에서 UI·상태표시·규제 재조회만 보완
- 배포 가능한 R14 후보본

