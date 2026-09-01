# R22 Station API resilience patch

- 기준본: `urban-strategy-v2.5.0-r22-station-rule-engine.zip`
- 앱 버전은 v2.5.0 유지.
- 서울 열린데이터광장 인증키가 존재해도 일시 API 오류/0건 응답이 프로세스 전체 수명 동안 캐시되던 `@lru_cache(maxsize=1)` 구조를 제거.
- `SearchSTNBySubwayLineInfo`를 1차 역-노선 Fact로 조회하고 `CardSubwayStatsNew`를 전 운영기관 노선 보강용 2차 소스로 사용.
- 성공 비어있지 않은 결과는 6시간, 빈/오류 결과는 60초만 캐시하여 자동 재시도 가능.
- CardSubwayStatsNew의 top-level/service-level RESULT 오류를 명시적으로 기록.
- `/api/reference/station-lines?force=1` 강제 재조회 지원.
- `/health`에 `seoul_open_data_configured`, `seoul_open_data_env` 추가(키 값은 노출하지 않음).
- 역사 UI 설명문에 노선 API 상태/역 수/첫 오류를 표시하여 `키 미인식`, `API 오류`, `부분자료`를 구분.
- `render.yaml`에 `SEOUL_OPEN_DATA_KEY` 명시. 기존 `data.seoul.go.kr_KEY`, `DATA_SEOUL_GO_KR_KEY` 호환은 유지.
