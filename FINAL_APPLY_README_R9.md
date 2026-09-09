# R9 CORS proxy patch 적용 안내

기준: `urban-strategy-v2.5.0-20260909-analysis-pipeline-r8-final-PATCH.zip`

## 목적
브라우저가 VWorld NED `ladfrlList` / `getLandCharacteristics`를 직접 `fetch(..., mode:'cors')` 하면서 발생하던 CORS 오류와 필지별 불필요한 재시도를 제거한다.

## 적용
기존 정상 저장소의 데이터 파일은 삭제하지 않는다. 이 PATCH의 `app.py`, `app.html`, `downtown_characteristic_management_derived.geojson`만 같은 경로에 덮어쓴다.

특히 다음 기존 데이터는 유지한다.
- `road_shp_seoul.zip`
- `basic_unit_seoul.zip` (보유 시)
- `stations.json`, `centers.json`
- `uq181_legal.zip`, `uq120_project.zip`
- 의료·비오톱·산지·학교보호 등 기존 기준자료

## R9 변경점
1. `fetchLandLedgerBrowser()`는 `/api/land/ledger-one` 서버 프록시를 1순위로 사용한다.
2. 브라우저 직접 XML CORS 호출을 제거했다.
3. 서버 `ledger-one`은 `ladfrlList → legacy data.go.kr → getLandCharacteristics` 순으로 공식정보를 보완한다.
4. `/api/land/characteristics-one` 서버 프록시를 추가했다.
5. `fetchOfficialAreaBrowser()`도 서버 프록시만 사용한다.
6. 서버가 정상적으로 VWorld 조회를 끝냈는데 기록이 없으면 브라우저에서 동일 조회를 반복하지 않는다.
7. 서버 프록시가 불가하거나 서버 VWorld 키가 없는 경우에만 JSONP를 최후 fallback으로 남겼다. JSONP는 `fetch` CORS 경로가 아니다.

## 변경하지 않은 것
- R8의 가로구역 선행·순차 실행 원칙
- 기초단위구 5초 preflight
- 역세권활성화 250/350m 판정
- 서울도심 특성관리지구 복원자료 및 배제판정
- 기존 사업별 RULE

## 배포 후 확인
브라우저 F12 → Console에서 `ladfrlList ... blocked by CORS policy`가 더 이상 반복되지 않아야 한다.
