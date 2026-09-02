# 패치: 도로 데이터 우선순위 반전 — 서버 내장(도로명주소 전자지도) 1순위, VWorld는 fallback

GPT 지적이 정확했다. 지난 패치에서 백엔드(`/api/spatial/roads`)는 도로명주소
전자지도 `TL_SPRD_MANAGE`를 쓸 수 있게 다 만들어놨는데, 프론트 `fetchRoadNetwork()`가
그걸 호출하지 않고 여전히 VWorld 실시간 호출만 하고 있었다. 그래서 VWorld가
무응답이면(지난번 "VWorld 무응답 · 도로중심선 0건" 화면) 서버에 이미 있는 데이터를
두고도 REVIEW로 빠지는 상태였다.

## 무엇을 바꿨나
`fetchRoadNetwork()`의 자료원 우선순위를 뒤집었다.

**1순위 — 서버 내장 도로명주소 전자지도**
```
POST /api/spatial/roads {geometry: activeGeometry}
```
응답의 `manage.features`(도로중심선, `ROAD_BT`/`RN`/`RDS_MAN_NO` 포함)를 그대로 씀.
이미 검수된 공식 원본이라 VWorld 응답 여부와 완전히 무관하게 항상 쓸 수 있다.

**2순위 — VWorld 실시간(서버 자료가 비어있거나 오류일 때만)**
기존 `trySpatialLayerCandidates(['TL_SPRD_MANAGE','LT_C_SPRD_MANAGE'], ...)` 호출은
그대로 남겨뒀고, 서버 내장 자료가 0건이거나 요청 자체가 실패했을 때만 실행된다.

두 경로 모두 결과는 **같은 `roadPolygonsFromCenterlines(mg)` 파이프라인**을 그대로
통과한다 — 진단 4단계(NO_RESPONSE/CENTERLINE_NO_WIDTH/PARTIAL_WIDTH/WIDTH_OK),
`ROAD_WIDTH_PROP_CANDIDATES` 공통 후보키, 대상지 접면 폭원 확보율(site_frontage),
PARTIAL_WIDTH caveat 등 지금까지 만든 진단엔진은 하나도 안 지우고 그대로 재사용된다
— 자료원만 바뀌고 처리 로직은 안 바뀌는 구조.

화면 출처 표시도 `도로명주소 전자지도 · TL_SPRD_MANAGE · ROAD_BT`로 나오고,
VWorld로 fallback된 경우에만 `(fallback)`이 붙어서 구분된다.

## 검증
- `node --check` PASS
- **실제 `POST /api/spatial/roads` 종단 테스트**(왕십리 인근 좌표) — 서버가 실제로
  `manage.features`에 `ROAD_BT:33.0`(왕십리광장로) 같은 실제 값을 돌려주는 것 확인
- `regression_checks.py` 전체 **41개 PASS**. `r11 data recovery fix1`은 예전엔
  "`fetchRoadNetwork`가 `/api/spatial/roads`를 쓰면 안 된다"는 그 시절 원칙을
  검증하던 테스트였는데, 이번 우선순위 반전이 의도적으로 그 원칙을 뒤집는 거라
  테스트도 "1순위로 쓰되 VWorld 실패 시에만 fallback한다"는 새 구조를 확인하도록 갱신함.

## 반영 방법
`app.html`, `regression_checks.py` 저장소 반영 후 재배포. app.py·데이터 파일은
지난 패치에서 이미 다 반영돼 있어 이번엔 안 건드림.

이제 VWorld가 응답하든 안 하든, 서울 시내 대상지는 내장 도로명주소 전자지도로
접도율이 나오게 된다.
