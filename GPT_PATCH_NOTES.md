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
