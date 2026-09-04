# GPT PATCH NOTES — r31 기준 실폭도로 제거 / TL_SPRD_MANAGE 단일화

기준본: `urban-strategy-v2.5.0-r31-safe-medical-performance-merged.zip`

## 변경 원칙
- 대용량 도로 polygon 번들을 배포본에서 제거한다.
- 도로 Fact는 VWorld `TL_SPRD_MANAGE` 도로중심선과 `ROAD_BT` 공식 폭원만 사용한다.
- `ROAD_BT`가 없거나 유효하지 않은 구간은 폭원을 임의 추정하지 않고 REVIEW로 남긴다.
- 도로중심선은 `ROAD_BT/2` 버퍼로 초기검토용 개략 도로범위를 만들고 접도연장·접도율을 산정한다.
- 실제 도로폭·도로구역·현황도로는 인허가/설계 단계에서 도로대장·결정도서·현장조사로 재확인한다.

## 삭제
- `road_seoul.zip` 삭제 (약 26MB)
- `app.py`의 로컬 도로 ZIP inventory/load/index/intersection API 삭제
- `/api/spatial/roads` 삭제
- `/api/spatial/road-data-status` 삭제
- polygon 형상에서 도로폭을 역산하는 backend fallback 삭제
- frontend의 도로 polygon 폭원 보정/매칭 helper 삭제

## 유지
- `TL_SPRD_MANAGE` + `ROAD_BT` 기반 도로폭 Fact
- 중심선 폭원 버퍼 기반 접도 개략분석
- 재개발/주거환경개선 접도율, 사업별 4·6·8·20·35m 폭원 Fact 구조
- 가로구역 후보분석의 `TL_SPRD_MANAGE ROAD_BT` 입력
- 도로위계/간선도로 기능이 별도 자료 없을 때 REVIEW로 남는 원칙

## 검증
- `python -m py_compile app.py regression_checks.py` PASS
- `app.html` 인라인 JavaScript `node --check` PASS
- 회귀검사: 도로 변경 이후 주요 항목 PASS. 전체 회귀검사는 기준본에 원래 포함되지 않은 공식 PDF/CHANGELOG 검사에서 중단되며 이번 도로 변경과 무관함.
- 패키지에 `road_seoul.zip` 없음 확인
- 실제 코드에서 삭제 대상 도로 polygon dataset 토큰/처리경로 없음 확인

앱 내부 버전은 사용자 원칙에 따라 `2.5.0` 유지.


# 추가 패치 — 사업구역 후보계 + 주택접도율 추후검토 전환

## 사업구역 후보계
- 사용자 검토요청지(`activeGeometry`)는 그대로 유지하고 별도 `projectBoundaryCandidateLayer`를 생성.
- 선택필지 중 외곽에 닿는 `지목=도로` 필지는 제외하여 도로의 대지측 경계를 우선 사용.
- 검토요청지 내부의 `지목=도로` 필지는 포함하여 내부가로가 후보구역을 다시 자르지 않도록 폐쇄.
- 선택필지 합집합이 MultiPolygon으로 남는 경우 지적 꼭짓점의 convex hull을 사용해 단부를 직선 폐합하고, 검토요청지 30m buffer 밖으로 과도하게 확장되지 않도록 제한.
- 내부 hole은 외곽 shell만 사용해 폐쇄.
- 도시계획시설 도로(`LT_C_UPISUQ151`) 교차 건수도 후보계 진단값에 기록.
- 지도에는 붉은 2점쇄선 형태의 `사업구역 후보계`로 표시하며, 사용자 원 구역계를 덮어쓰지 않음.

## 주택접도율
- 재개발·주거환경개선의 1차 사업후보 자동판정 변수에서 제외.
- 법정 세부항목·원자료·수기입력 기능은 유지하고 결과표에서는 `추후검토/INFO`로 표시.
- 재개발 추가 자동판정은 과소필지·호수밀도만 사용.
- 비관리형 주거환경개선 추가 자동판정은 노후도·과소필지만 사용.
- 접도율은 정비계획 단계의 현장조사·도로대장·도면검토로 이관.
