# CLAUDE_ROAD_PATCH.md — 도로·간선도로·접도 패치 (r31 기준본 위)

## 수정 파일
- `app.html`만 수정. `app.py`·`road_seoul.zip`·의료시설 관련 파일은 전혀 건드리지 않음.

## 근본원인
`road_seoul.zip`이 `TL_SPRD_MANAGE`(중심선)에서 `TL_SPRD_RW`(실폭도로 polygon)로
바뀌었는데, 프론트 `fetchRoadNetwork()`는 여전히 `bundled.manage.features`만 읽고
있었다. `POST /api/spatial/roads`를 직접 재현해보면 `manage:0 / rw:2`(왕십리 테스트) —
즉 위치와 무관하게 항상 "서버 내장 도로자료 0건"으로 보였던 원인이 바로 이거였다.

## 수정 함수

**`fetchRoadNetwork(radiusM)`** — `bundled.rw.features`를 1순위로 읽도록 변경.
rw 없으면 `bundled.manage.features`, 그것도 없으면 기존 VWorld `TL_SPRD_MANAGE`
fallback 순서 유지.

**`annotateRoadWidths(rwFeatures,manageFeatures)`** — RW polygon엔 `ROAD_BT` 속성이
없다(폭이 형상 자체에 담김). ROAD_BT 없고 manage 교차도 안 되면
`estimateLocalRoadPolygonWidthMeters()`로 국부폭을 추정하고, `_width_estimated=true`로
표시해서 이후 안전마진 판정에 쓸 수 있게 함.

**`siteRoadNetworkStats(site,roads)`** — 그룹별 `width_estimated` 필드 추가(최대폭을
정한 세그먼트가 추정치인지 추적). `has8`/`has20Width`는 추정치가 경계값(±0.5m) 안에
있으면 `null`(REVIEW)로 강등 — ROAD_BT 같은 공부상 속성값에는 이 마진 미적용.

## 추가 함수

**`estimateLocalRoadPolygonWidthMeters(roadFeature,siteFeature,zoneRadiusM=12)`** —
GPT 검토 합의사항 반영: 도로 polygon 전체 면적/둘레로 폭을 내면 교차로·확폭부·광장형
구간이 섞여 평균이 왜곡된다. 대상지 12m 반경으로 잘라낸 조각에서만
`estimateRoadPolygonWidthMeters()`를 다시 돌려 국부폭을 낸다. 실패하면 전체 폴리곤
값으로 폴백.

**`isNearRoadWidthThreshold(widthM,thresholdM)`** + `ROAD_WIDTH_SAFETY_MARGIN_M=0.5`
— GPT 검토 합의사항 반영: 추정폭이 법정 경계값(8m/20m)에 바짝 붙으면(예: 8.1m)
확정판정하지 않는다.

**`roadPolygonsFromRealWidth(rwFeatures,manageFeatures)`** — RW polygon 전용 진단
엔진. RW는 이미 실제 도로 형상이라 중심선처럼 버퍼링하지 않는다(`turf.buffer(mf,w/2)`
로직 없음). `annotateRoadWidths()`를 그대로 호출해서 폭을 구하고, 기존
`roadPolygonsFromCenterlines()`와 같은 필드명(centerline_count/buffer_success_count 등)
으로 4단계 진단(NO_RESPONSE/CENTERLINE_NO_WIDTH/PARTIAL_WIDTH/WIDTH_OK)을 유지 —
기존 진단 UI(`renderRoadDiagnostic`)를 재작성하지 않고 그대로 재사용.

## 삭제 함수
없음. 기존 `roadPolygonsFromCenterlines()`/`roadWidthFromManage()`는 VWorld
fallback(중심선+ROAD_BT) 경로에서 그대로 유지.

## 수정 이유
1. manage/rw 키 불일치로 도로 데이터가 항상 0건으로 보이던 버그 수정 (근본원인)
2. GPT 리뷰 합의사항 ①: 폴리곤 전체 평균폭 대신 대상지 인근 국부폭 우선 사용
3. GPT 리뷰 합의사항 ①: 추정폭이 경계값 근처면 자동 PASS/FAIL 금지, REVIEW로 강등

## 건드리지 않은 영역
- 의료시설(`safe_medical_reference.json`, `TbHospitalInfo`, PNU resolver, 350m buffer) —
  GPT 담당 영역, 전혀 미수정
- `app.py`, `/api/spatial/roads` 백엔드 로직 — 이미 rw/manage 둘 다 정상 반환 중이라 불필요
- `commonSchemeData()`의 자동값 우선 로직(road4Faces/has8/has20Width/road20Perimeter) —
  이전 세션에 이미 자동값 우선으로 연결돼 있었고 이번 패치로 값 소스(`analysisState.
  road_network`)만 더 정확해짐, 연결 자체는 안 건드림
- 재개발 주택접도율(`schemeFrontageEvidenceFacts`의 `net.frontage_basis_buildings` 등) —
  이미 대상지 접면수(`road4Faces`)와 분리돼 있었음, 그대로 유지

## 사용 데이터
`road_seoul.zip`(TL_SPRD_RW 실폭도로, 60,534건) — 변경 없음, 그대로 사용.

## 실제 테스트 대상
배정받은 역삼동 689-1 / 신사동 630-19 / 사당동 1019-46은 지오코딩 수단이 없어 정확한
좌표를 못 구했다. 대신 이미 검증된 실좌표(왕십리 인근, `127.037,37.561` 부근 소구역)로
아래를 실행했다.

## 런타임 결과 (실제 실행값)

1. **국부폭 vs 전체폭 왜곡 확인(합성 테스트)**: 100m×8m 도로 + 멀리 떨어진 50m×30m
   확폭부를 하나의 MultiPolygon으로 합쳤을 때 — 전체폭 추정 13.19m(왜곡), 대상지
   12m 반경 국부폭 추정 8.07m(정답 8.03m와 거의 일치, `local:true`).
2. **실제 왕십리 도로 데이터**: `POST /api/spatial/roads` 응답의 RW feature 2건에
   대해 전체폭 21.61m/9.47m, 국부폭 30.40m/11.20m — 국부 클리핑이 실제 데이터에서도
   서로 다른 값을 냄(둘 다 `local:true`).
3. **안전마진 로직**: 8.1m(경계 근접)→uncertain, 8.6m(안전)→확정, 7.6m(경계 근접)→
   uncertain, 19.9m(경계 근접)→uncertain — 4개 케이스 모두 의도대로 동작.
4. turf.js는 앱이 실제 로드하는 **6.5.0**으로 맞춰서 검증(npm 최신 7.x는 `intersect()`
   API가 달라 처음에 오탐이 났었음 — 버전 맞춰 재검증).

## ESTIMATE / CONFIRMED 구분
- `_width_estimated=true`: RW polygon 형상 기반 국부폭 추정(법정 확정 아님)
- `_width_estimated=false`: ROAD_BT 등 공부상 속성값(기존과 동일 신뢰도)
- `has8`/`has20Width`가 `null`이면: 추정치가 경계값 근처라 REVIEW로 강등된 상태

## 검증
- `node --check` PASS
- `regression_checks.py` 전체 **49개 PASS** (신규 `r31 road local width safety margin`
  포함, 기존 2개 테스트는 아키텍처 반전에 맞춰 마커 갱신)
