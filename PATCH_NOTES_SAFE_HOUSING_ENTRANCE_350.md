# 패치: 안심주택 350m 예외경로에 출입구 통합범위 반영

GPT 스펙(안심주택 350m 예외경로 보완) 그대로 구현했다. 250m 일반경로·우선순위 구조는
그대로 두고 350m 예외경로만 승강장 경계 + 연결된 출입구 350m 통합범위로 바꿨다.

## 데이터 — 새로 만든 것
`station_entrances.json` (283개 역, 1,585개 출입구). juso.go.kr 원본
`TL_SPSB_ENTRC`(출입구, 1,743건)을 배포 전 오프라인 전처리로 `stations.json`(내장
역사 폴리곤, 349개)에 공간 최근접 매칭했다.

**중요하게 확인한 것**: `TL_SPSB_ENTRC`엔 소속 역을 가리키는 속성 키가 없다
(`SIG_CD`/`SUB_ENT_SN`/`ENTRC_NO`/`OPERT_DE`뿐). `SUB_ENT_SN`도 전역 유일키가
아니었다(1,743건 중 1,463개만 유일 — 117개 값이 여러 역에서 중복 사용됨, 아마
역별 순번). 그래서:
- 매칭은 **공간 최근접**으로만 했다(반경 300m 이내).
- 최근접 역과 차근접 역의 거리 차이가 애매하면(margin 50m 미만이면서 비율 1.5배
  미만) **매칭하지 않고 제외**했다 — 158건이 이렇게 빠졌다.
- 전역 유일 `entrance_id`를 새로 부여했다(원본 `SUB_ENT_SN`은 참고 속성으로만 유지).
- 역명 정규화 등으로 런타임에 추가 매칭을 시도하지 않는다 — 전처리 결과를 그대로 신뢰.

## 코드 — 새로 추가한 것 (app.html)
- `stationEntranceMap`, `loadStationEntrances()` — 서버에서 역명→출입구 목록 로드
- `safeEntranceFeaturesForStation(stationFact)` — 그 역과 공식 연결된 출입구만 반환
- `safeStation350Geometry(stationFact)` — 승강장 경계 350m 버퍼 ∪ 연결된 출입구 350m 버퍼
- `coverageOfSite(siteGeometry, unionGeometry)` — 대상지 포함률(%) 계산
- `safeStation350Facts()` / `bestSafe350()` — 안심주택 전용 350m 역별 판정 목록

## 코드 — 바꾼 것
`safeStationPath(c)`: 250m 일반경로(`eligible250`/`overlap250`)는 그대로. 350m
경로(`eligible350`/`overlap350`)만 `bestStationByCoverage(350)`(단순 승강장 버퍼) →
`bestSafe350()`(승강장+출입구 통합범위)로 교체. 우선순위(250 과반 → 250 일부 →
350 과반 → 350 일부 → FAIL)와 "350m은 절대 자동 PASS 안 됨" 원칙은 그대로 유지.
결과 문구도 "승강장 경계 + 출입구 N개 통합범위"처럼 구분해서 표시.

`safeHousingSpatialFacts()`: 공간현황에 "역세권 350m 예외경로 상세" row를 새로
추가 — 기준 geometry, 출입구 연결 개수, 350m 통합 포함률을 별도로 보여준다.

## 다른 사업방식은 안 건드림
역세권활성화·장기전세·역세권복합개발·도심공공주택복합·도심복합개발은 여전히
기존 `bestStationByCoverage(350)`/`coverage350`을 그대로 쓴다. `safeStation350Geometry`/
`safeEntranceFeaturesForStation`/`bestSafe350`은 안심주택 코드 블록 밖에서 전혀
참조되지 않는다 — 이건 정적 검사로 확인했다(아래).

## 백엔드 (app.py)
- `_station_entrance_reference_data()` — 위 JSON 로더(lru_cache)
- `GET /api/reference/station-entrances` — 프론트에 그대로 서빙

## 검증
- `node --check` PASS
- `python -m py_compile app.py` PASS
- `regression_checks.py` 전체 **41개** PASS — 기존 40개 + 신규
  `safe housing entrance 350m` 1개. 신규 테스트가 확인하는 것:
  - 신규 함수 전부 존재
  - 출입구 매칭은 이름 재정규화 없이 서버 전처리 결과만 사용(`stationNameKey` 미참조)
  - 250m 경로 우선순위·원본 함수 구조 보존
  - **다른 5개 사업 모듈(역세권활성화/역세권복합/장기전세/도심공공주택복합/
    도심복합개발) 블록 안에 안심주택 전용 함수가 전혀 안 들어있는지** 확인
  - `station_entrances.json` 자체의 데이터 품질(entrance_id 전역유일, 원본
    1,743건보다 매칭건수가 실제로 적은지=애매한 것들이 진짜 빠졌는지)

## 반영 방법
`app.html`, `app.py`, `regression_checks.py`, `station_entrances.json` 4개 파일을
저장소에 반영 후 재배포. `render.yaml`/Dockerfile 변경 불필요(정적 파일 하나
추가되는 것뿐).

## 안 한 것
GPT 스펙 중 "공장용도 현황분석 공간현황 모듈"(장기전세·도심복합 주거중심형 공장비율)은
이번 패치에 포함하지 않았다 — 이번엔 안심주택만 요청받아서 그것만 했다.
