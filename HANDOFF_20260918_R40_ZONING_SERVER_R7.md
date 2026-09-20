# R40 ZONING SERVER R7 인수인계

## 기준본
`urban-strategy-v2.5.0-20260918-r40-REGULATORY-WIP-ZONING-FACT-R6.zip`

## 변경 목적
판정용 용도지역 `LT_C_UQ111`만 서버 프록시로 이전하여 브라우저 JSONP 직접조회 불안정을 제거한다.

## 핵심 변경
- 신규 endpoint: `POST /api/spatial/zoning`
- 입력: `{ "geometry": <GeoJSON Polygon|MultiPolygon> }`
- 출력: `known`, `status`, `features`, `cache_hit`, `cache_age_sec`, `source`, 필요 시 `warning/message`
- fresh cache 10분, 장애 시 stale fallback 최대 1시간
- 0건은 NONE/PASS 근거로 사용하지 않음
- HTTP 429는 429로 유지하여 기존 cooldown과 연계
- 프론트 `fetchPlanningSpec()`은 UQ111만 서버 endpoint 사용
- `analyzePlanningGIS()`는 용도지역을 우선 조회

## 변경하지 않은 것
사업별 RULE, 추천, 밀도, 가로구역, 사업구역, 접도, 역세권, 도시재생혁신지구 SHP, 나머지 도시계획 22개 레이어.

## 다음 확인
배포 후 기존 5,093㎡ 테스트 대상지를 동일 조건으로 2~3회 연속 분석하여 용도지역 확보 및 cache 동작을 확인한다.
