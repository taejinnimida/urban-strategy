# MERGE_CHANGELOG.md — r31 기준본 + 도로패치 + 의료시설 대표필지 개선

## 기준본
`urban-strategy-v2.5.0-r31-safe-medical-performance-merged.zip`

## 바뀐 파일
`app.py`, `app.html`, `regression_checks.py`만 교체. 나머지(데이터 파일, PDF,
render.yaml, Dockerfile 등)는 r31 그대로 재사용하면 됨.

## 1. 도로 패치 (app.html) — 상세는 CLAUDE_ROAD_PATCH.md 참고
- **근본버그**: `road_seoul.zip`이 TL_SPRD_MANAGE→TL_SPRD_RW로 바뀌었는데 프론트가
  여전히 `bundled.manage.features`만 읽어서 항상 "도로 0건"으로 보이던 문제 수정
- 국부폭 추정(`estimateLocalRoadPolygonWidthMeters`) — 교차로·확폭부가 평균폭을
  왜곡하지 않도록 대상지 인근만 잘라서 추정
- 안전마진(`ROAD_WIDTH_SAFETY_MARGIN_M=0.5`) — 추정폭이 8m/20m 경계값에 바짝 붙으면
  자동 PASS/FAIL 대신 REVIEW

## 2. 의료시설 대표필지 개선 (app.py)
기존 r31에 이미 있던 것(변경 안 함): 종합병원/시립병원은 도시계획시설 종합의료시설
경계 → 건축물대장 총괄표제부+부속지번 합필 경계 복원 → 단일필지 순으로 3단계 판정.
보건소는 단일 대표필지.

**이번에 추가한 것**: 좌표·주소 기반 `covers(point)` 필지매칭이 둘 다 실패했을 때
(시설 좌표가 도로변·출입구·필지경계 근처인 경우), 반경 15m 안 근접 후보 필지를:
- **종합병원/시립병원**: 기존 건축물대장 검증 파이프라인(병원명·의료시설 용도
  매칭, 부속지번 개수 확인)에 추가 후보로 투입 — 검증을 통과해야만 CONFIRMED로
  승격. 단순히 "가깝다"는 이유로 자동확정하지 않음(GPT 검토 합의사항).
- **보건소**: 검증 레이어가 없어 자동확정에는 안 쓰고, `nearest_parcel_candidates`로
  REVIEW 화면에 참고용 후보만 노출.

신규 함수: `_vworld_nearest_parcel_candidates(lon,lat,max_distance_m=15,limit=3)`.

## 검증
- `python -m py_compile app.py` PASS
- `node --check`(app.html 인라인 JS) PASS
- `regression_checks.py` 전체 **49개 PASS**
- 도로: 왕십리 실좌표로 `/api/spatial/roads` 종단검증(국부폭 vs 전체폭 실측값 차이 확인)
- 의료시설: `_vworld_nearest_parcel_candidates`를 목데이터로 거리계산·정렬·반경필터
  로직 검증(실제 VWorld/건축HUB API는 이 샌드박스에서 호출 불가)

## ESTIMATE / CONFIRMED 구분
- 도로: `_width_estimated=true`(RW 형상 기반 국부폭 추정) vs `false`(ROAD_BT 공부상 값)
- 의료시설: `boundary_basis`가 `BUILDING_REGISTER_SITE_PARCELS`/`CADASTRAL_PARCEL_
  FROM_OFFICIAL_POINT`면 CONFIRMED, `nearest_candidate_unverified` 경유는 건축물대장
  검증까지 통과해야 CONFIRMED(그렇지 않으면 REVIEW로 남음)

## 남은 개략판정 항목 (라이브 재검증 필요)
- 배정받은 3개 테스트 주소(역삼동 689-1/신사동 630-19/사당동 1019-46)는 지오코딩
  수단이 없어 실좌표로 재현 못 함 — 왕십리 실좌표로 대체 검증함
- 의료시설 근접후보 로직은 목데이터 검증까지만 완료 — 실제 VWorld 응답으로 확인 필요
  (중구보건소 인근 대상지로 `POST /api/reference/safe-medical-nearby` 응답의
  `boundary_status`/`boundary_basis`/`nearest_parcel_candidates` 확인 요망)
- 가로주택정비 통과도로 예외(1만~2만㎡ 구간 4m→6m 완화)의 정확한 항·호는 GPT
  후속 조사 대기 중 — 아직 코드 미반영
