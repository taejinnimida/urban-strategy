# R9 CORS proxy 검증기록 — 2026-09-09

## 발견 증상
배포 페이지 브라우저 콘솔에서 선택 PNU마다 다음 오류가 반복되었다.
- `api.vworld.kr/ned/data/ladfrlList`
- `blocked by CORS policy`
- `No 'Access-Control-Allow-Origin' header`
- `net::ERR_FAILED`

원인은 브라우저가 VWorld NED를 직접 `fetch(..., mode:'cors')` 한 뒤 실패하고, 이후 fallback을 다시 수행하는 구조였다.

## 수정
- 브라우저 NED 직접 CORS fetch 전면 제거
- `/api/land/ledger-one` 서버 프록시 우선
- `/api/land/characteristics-one` 추가
- 서버 fallback: VWorld `ladfrlList` → legacy data.go.kr → VWorld `getLandCharacteristics`
- 서버에서 정상 조회 완료 후 record 없음이면 동일 브라우저 조회 반복 방지
- 서버 프록시 불가/키 미설정 시에만 JSONP 최후 fallback

## 검증 결과
- `python -m py_compile app.py`: PASS
- 전체 inline JS `node --check`: PASS
- `GET /`: 200
- `GET /health`: 200
- R8 targeted regression: ALL PASS
- R5 targeted regression: ALL PASS
- R6 targeted regression: ALL PASS (기존 road bundle을 테스트환경에 임시 제공)
- 신규 R9 targeted regression: ALL PASS
- `mode:'cors'` / `mode:"cors"` 문자열: 0건
- 브라우저 직접 VWorld fetch 패턴: 0건
- 동적 테스트:
  - ledger 성공 시 legacy/characteristics 미호출: PASS
  - ledger+legacy 실패 시 characteristics fallback: PASS
  - `/api/land/characteristics-one`: 200 및 record 반환: PASS

## 범위
이번 수정은 CORS 및 토지대장/토지특성 조회 경로만 변경했다. 가로구역 계산 순서와 사업판정 RULE은 변경하지 않았다.
