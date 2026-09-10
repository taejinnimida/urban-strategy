# R16 검증기록

## 변경범위
프론트 `app.html`만 수정. R15 백엔드 `app.py`는 byte/hash 동일하게 유지.

## 실행검증
- inline JavaScript 추출 후 `node --check`: PASS
- R15 `app.py`: `python -m py_compile app.py`: PASS
- R15 토지대장 성능 targeted regression: 15/15 PASS
- R16 targeted regression: 18/18 PASS

## R16 targeted 확인항목
- REVIEW → `확인필요`
- 실제 conditional만 조건부 표현
- 가로주택 후보 ranking에 `coverage` 모드 존재
- smallscale만 coverage 우선, 역세권계열은 기존 share 우선 유지
- `multi_block` 존재만으로 가로주택 하드 FAIL하지 않음
- 검토구역 편입률 / 가로구역 점유율 분리
- 역세권활성화 `route.selected` / `route.available` 분리
- 도시정비형 독립 / 정책사업 연계 2축 판정
- 기존 `정책사업 하위/직접진입 미확인` 강제 OFF 제거
- 14개 정비가능구역 공식 GIS 미연결 상태에서 임의 PASS 금지 유지

## 유의
실제 청구역 대상지에서 가로주택정비가 PASS로 바뀌는지는 서버가 반환한 후보 블록 중 검토구역을 사실상 전부 포함하는 후보가 존재하는지에 달려 있다. R16은 그 후보가 있는데도 `가로구역 점유율` 우선 정렬 때문에 3,303㎡ 소블록을 선택하던 구조를 수정한다. 후보 자체가 생성되지 않는 경우에는 가로구역 복원 엔진을 추가 진단해야 한다.
