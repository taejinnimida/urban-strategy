# R40 ZONING SERVER R7 검증보고서

기준본: `urban-strategy-v2.5.0-20260918-r40-REGULATORY-WIP-ZONING-FACT-R6.zip`

## 목적
사업판정의 핵심 FACT인 `LT_C_UQ111 용도지역`을 브라우저 VWorld JSONP 직접조회에서 FastAPI 서버 프록시 조회로 전환한다. 기존 사업판정 RULE 및 다른 공간분석은 변경하지 않는다.

## 확인된 기존 원인
- R6까지 `LT_C_UQ111`은 `fetchSpatialFeaturesBrowser()`를 통해 브라우저에서 VWorld JSONP를 직접 호출했다.
- R5의 429 cooldown / non-JSON 방어 / same-origin API pacing은 `/api/...` 요청에 적용되므로 UQ111은 해당 안정화 범위 밖에 있었다.
- VWorld `NOT_FOUND` 또는 일시 미응답이 UQ111 0건으로 귀결되면 핵심 FACT가 미확보되어 사업판정이 보류됐다.

## 변경사항
1. FastAPI `POST /api/spatial/zoning` 추가.
2. 서버에서 `LT_C_UQ111`을 `_vworld_features_in_bbox()`로 조회하여 기존 direct→official proxy 경로를 사용.
3. 같은 geometry의 정상 UQ111 결과는 10분 fresh cache 유지.
4. 10분 이후 재조회 실패 시 최대 1시간 이내 최근 정상 FACT를 stale fallback으로 사용.
5. UQ111 0건은 정상값으로 캐시하지 않고 `known=false`로 유지.
6. VWorld HTTP 429는 서버가 429로 전달하여 기존 프론트 cooldown이 작동하도록 함.
7. `fetchPlanningSpec()`은 UQ111에 한해 `/api/spatial/zoning`을 사용하고 나머지 22개 레이어는 기존 브라우저 경로 유지.
8. `analyzePlanningGIS()`는 UQ111을 먼저 단독 확보한 뒤 시설/용도지구/지구단위계획을 조회.
9. 기존 R6의 `용도지역 핵심 FACT 미확보 → 사업판정 보류` 원칙 유지.

## 회귀검사
- Urban regeneration integrated R3: 27/27 PASS
- Activation / prior-negotiation competition R4: 30/30 PASS
- Analysis stability R5: 19/19 PASS
- Zoning FACT gate R6: 19/19 PASS
- Zoning server R7: 20/20 PASS

## R7 런타임 단위검사
- 최초 UQ111 서버조회 성공: PASS
- 동일 geometry 2회차 외부호출 없이 cache hit: PASS
- fresh TTL 이후 VWorld 502 가정 시 최근 정상 FACT stale fallback: PASS
- JavaScript syntax: PASS
- app.py compile: PASS

## 보존 확인
다음 핵심 함수는 R6 대비 byte-identical:
- `densityForScheme()`
- `checkActivationFromFacts()`
- `checkPriorNegotiationFromFacts()`
- `analyzeSchemeStreetBlocks()`
- `analyzeRoadAccess()`
- `runAllSchemeChecks()`
- `runAllAutoAnalyses()`

## 배포 후 확인사항
동일 5,093㎡ 테스트 대상지를 검토한다.
- 용도지역 정상 확보 시 `용도지역 핵심 FACT`가 정상 완료되어야 한다.
- 같은 대상지 재검토 시 UQ111은 서버 캐시를 이용할 수 있다.
- 외부 일시장애가 있어도 최근 정상 UQ111이 있으면 해당 FACT를 유지해야 한다.
- 첫 조회부터 UQ111이 실제 미확보인 경우에는 기존 R6 원칙대로 사업판정을 보류해야 한다.
