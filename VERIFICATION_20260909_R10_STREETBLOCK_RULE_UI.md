# R10 가로구역 규칙·UI 최종 검증

## 기준
- 기준 코드: R9 CORS proxy patch
- 변경 범위: 제도별 가로구역 공간추출 규칙 + 가로구역 검토 UI
- 유지: 가로구역 선행·순차실행, R9 토지정보 server-proxy, 역세권 250/350m, 특성관리지구, 성장잠재권 35m/6m 별도 입지판정

## 1. 가로주택정비 가로구역 — 현재 시행규칙에 맞춰 정정
최종 법령 확인 과정에서 과거 프로젝트의 “도시계획도로도 6m 이상” 가정을 정정했다.

2026.2.27 시행 「빈집 및 소규모주택 정비에 관한 특례법 시행규칙」 제2조제3항에 따르면:
- 도시·군계획시설인 도로 및 지형도면 고시 도로: 폭원 조건 없이 가로구역 경계 도로
- 건축법상 도로 및 같은 조에서 정한 기타 도로: 6m 이상 기준
- 기반시설/예정기반시설: 공용주차장, 광장, 공원, 녹지, 공공공지, 하천, 철도, 학교 등

R10 구현:
- `existing_min_m:6`
- `planning_mode:'all'`, `planning_min_m:null`
- 공용주차장·광장·공원·녹지·공공공지·하천·철도·학교를 제도별 facility cutter로 사용
- 도시계획도로와 법정 시설을 단순 사후 제척만 하지 않고 `/api/spatial/street-block`의 기초단위구 병합 단계에 topology barrier로 전달
- 내부에 고립된 작은 시설은 무조건 블록을 쪼개지 않고, 실제로 블록을 관통·분리하거나 외곽 폐합에 기여하는 시설만 backend에서 경계로 승격

따라서 목동사거리처럼 현재 도시계획시설 광장이 있는 곳에서 광장을 무시하고 과거 지적형상처럼 블록이 연결되는 오류를 줄이도록 수정했다.

## 2. 성장잠재권 가로구역
「서울특별시 성장잠재권 활성화사업 운영기준」 1-3-13은 가로구역을 도로 또는 공용주차장·광장·공원·녹지·공공공지·하천·철도·학교로 둘러싸인 일단의 지역으로 정의한다.

R10 구현:
- `existing_min_m:0`
- `all_roads_boundary:true`
- 도로 폭원과 무관하게 모든 현황 도로를 가로구역 topology 경계로 사용
- 폭원 정보가 누락된 도로면/중심선도 growth topology에서 버리지 않도록 fallback 보강
- 공용주차장·광장·공원·녹지·공공공지·하천·철도·학교를 경계시설로 사용
- 운영기준에 명시되지 않은 미개설 도시계획도로 separator는 임의 추가하지 않음 (`planning_mode:'none'`)

별도 입지 도로요건은 그대로 유지:
- 2면 이상 폭 6m 이상 도로 접도
- 대상지 전체 둘레의 1/8(12.5%) 이상을 폭 35m 이상 간선도로에 접도
- 35m 간선도로 해당 여부는 별도 FACT/RULE

즉 6m는 성장잠재권의 가로구역 “폐합 정의”가 아니라 사업대상지의 별도 접도요건이다.

## 3. 성장잠재권 진단도면
- 35m 이상 도로를 street-block 단계에서 확보한 도로 FACT로 강조
- 대상지 35m 접면율 표시
- 비정상 대형 후보가 생성되어도 법적 산정값은 바꾸지 않고, mini-map 표시범위만 대상지 250m 주변으로 제한
- 후보블록 면적이 200,000㎡ 초과 또는 대상지의 25배 초과일 때 display clamp 적용

## 4. 가로구역 UI
대형 화면: 4개 제도를 한 줄 4칸으로 동시 비교
1. 가로주택정비
2. 역세권활성화
3. 역세권복합개발
4. 성장잠재권

반응형:
- 1450px 이하: 2칸
- 900px 이하: 1칸

## 5. 검증
- Python `py_compile`: PASS
- 전체 inline JavaScript `node --check`: PASS
- R5 targeted regression: PASS
- R6 targeted regression: PASS (기존 정상 기준자료의 road ZIP을 검증 중에만 임시 제공)
- R8 targeted regression: PASS
- R9 targeted regression: PASS
- R10 targeted regression: PASS
- synthetic topology test: 관통 시설 = barrier / 내부 고립 시설 = non-barrier PASS
- FastAPI `/`: 200
- FastAPI `/health`: 200

원본 `regression_checks.py` 전체 PASS는 이번 패치 단독환경에서 주장하지 않는다. 전체 기준자료가 포함된 실제 저장소 환경에서 별도로 수행해야 한다.

## 6. 성능/실행순서
대형 공간자료를 동시에 적재하지 않는 기존 설계를 유지한다.
- 제도별 가로구역을 가장 먼저 완료
- 이후 다른 FACT 분석
- `basic_unit_seoul.zip` 미설치 시 preflight로 즉시 REVIEW 후 다음 분석

## 적용 주의
이 ZIP은 코드 패치다. 기존 저장소의 `road_shp_seoul.zip`, `basic_unit_seoul.zip`, stations/centers, UQ181/UQ120, 의료시설·비오톱·산지·학교보호 등 기존 기준자료를 삭제하지 않는다.
