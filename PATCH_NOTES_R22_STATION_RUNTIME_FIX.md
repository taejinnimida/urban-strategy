# R22 Station Runtime Fix

- 기준본: `urban-strategy-v2.5.0-r22-station-rule-engine.zip` (현재 배포 app.py와 동일 계열)
- `/api/reference/station-line/{station_name}` 직접조회 추가
- `/api/reference/station-lines?force=1` 강제 재조회 지원
- 실패/키 미설정 결과의 장기 `lru_cache` 제거, 성공값만 TTL 캐시
- `/health`에 `build_marker`, `seoul_open_data_configured`, `seoul_open_data_env` 추가
- Render Blueprint에 표준 환경변수 `SEOUL_OPEN_DATA_KEY` 선언. 기존 `data.seoul.go.kr_KEY`도 코드에서 계속 허용
- 서울교통공사 역-노선표를 먼저 조회하고 CardSubwayStatsNew로 타 운영기관 노선 보강
- 1개 노선만 확인된 경우 비환승 확정 금지
- 성장거점형은 `transfer===true && line_data_complete===true`인 후보만 환승결절 판정역으로 사용
- 후보역마다 직접조회 fallback 추가. 왕십리는 2·5호선 확인만으로도 성장거점형 환승결절을 확정할 수 있음
