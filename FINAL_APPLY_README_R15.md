# R15 적용안내

R14를 적용한 현재 저장소에서 **app.py만 R15 app.py로 교체**한다.
app.html, road_shp_seoul.zip, basic_unit_seoul.zip 및 기존 기준자료는 삭제/교체하지 않는다.

변경범위는 토지대장 `/api/land/ledger-one`의 성능경로뿐이다.
- VWorld full ledger + data.go full ledger 동시 시작
- full ledger 우선, characteristics fallback 유지
- land-ledger timeout 축소
- data.go HTTP/HTTPS 중복 retry 제거
- PNU 성공/실패 TTL cache 추가

기존 사업 판정 Rule, 가로구역 batch, R14 UI는 변경하지 않는다.

배포 후 같은 98필지 검토에서 토지대장 단계 시간을 다시 측정할 것.
