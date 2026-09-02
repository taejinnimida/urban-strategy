# 패치: 도로 진단 엔진 v2 — GPT 리뷰 3개 반영

v1(진단 4단계 도입) 이후 GPT가 v1 코드를 직접 리뷰하며 짚은 세 가지를 전부 반영했다.
GPT 자신의 별도 수정안(app_4_.html)은 진단 세분화는 더 잘했지만 회귀테스트 마커
문구를 건드려 `r21` 테스트를 깨뜨렸고, `roadWidthFromManage` 폭원키 통일도
되돌려놓은 상태였다 — 그건 반영하지 않고, v1(이 파일 계열)을 베이스로 세 가지만 보강했다.

## 반영한 세 가지

1. **valid_width_count ≠ derived 개수 어긋남 수정**
   기존엔 폭원값+Line geometry가 유효하면 바로 카운트하고 그 뒤 `turf.buffer()`가
   실패해도 이미 카운트에 잡혀 있었다. 이제 `buffer_success_count`(버퍼까지 성공)와
   `buffer_fail_count`(폭원은 유효했으나 버퍼 생성 실패)를 분리했고, `buffer_success_count`가
   실제 `derived.length`와 항상 일치하도록 카운팅 순서를 버퍼 성공 이후로 옮겼다.

2. **"폭원 없음" 사유를 4가지로 분리**
   기존 `missing_width_count` 하나에 뭉쳐 있던 사유를 다음 4개로 나눴다:
   - `non_line_geometry_count`: 애초에 도로중심선(Line) geometry가 아님
   - `missing_width_count`: 후보키(`ROAD_BT`/`ROAD_WIDTH`/`ROAD_W`/`WIDTH`) 자체가 없음
   - `invalid_width_count`: 후보키는 있는데 값이 1~100 범위 밖(파싱 실패/이상값)
   - `buffer_fail_count`: 폭원은 유효했으나 버퍼 생성만 실패
   PARTIAL_WIDTH 판정 분모도 `line_feature_count`(Line geometry인 것만)로 맞췄다.

3. **대상지 접면 폭원 확보율을 별도로 계산**
   검색반경(240m) 전체의 폭원 확보율과, **대상지 경계에서 실제로 가까운(기본 15m
   이내) 도로중심선만의 폭원 확보율**을 분리했다. `computeSiteFrontageWidthCoverage()`가
   `siteFrontageCenterlineDistance()`로 각 중심선-대상지 경계 최단거리를 재서, 근접한
   것만 골라 `frontage_centerline_count`/`frontage_width_confirmed_count`/`coverage_pct`를
   낸다. 예: 전체는 37건 중 22건(59%)이어도, 실제 접면 4건이 전부 폭원 확인되면 그건
   접도판정엔 문제없다는 걸 화면에서 바로 구분할 수 있다. PARTIAL_WIDTH일 때는
   "이 수치만으로 접도요건 충족/불충족을 확정하지 않는다"는 caveat도 현황해석 문구에
   자동으로 붙는다(구조적으로도 `frontage.dataset.autoRoad`가 이 경로에서 항상
   `'ESTIMATE'`로만 세팅되므로 자동 PASS 자체가 원래도 안 열려 있었다).

## 바뀐 파일
- app.html (`roadPolygonsFromCenterlines`, `fetchRoadNetwork`,
  `computeSiteFrontageWidthCoverage`/`siteFrontageCenterlineDistance`(신규),
  `renderRoadDiagnostic`, `renderRoadConditionReview`, 진단 패널 HTML에
  "대상지 접면 폭원 확보율" 행 추가)

## 검증
- `node --check` PASS
- `regression_checks.py` 전체 39개 PASS (`r21 single boundary + sequential diagnostics`
  포함 — GPT 별도안에서 깨졌던 그 테스트)

## 반영 방법
저장소 `app.html` 교체 후 재배포. app.py·데이터 파일 변경 없음.
