# 검증보고서 — R40 도시재생 SHP 통합 R3

## 확인된 기존 오류
배포화면에서 `도시재생활성화지역 · urban_regeneration_innovation_reference.geojson 미설치`가 표시되었다. 이는 도시재생혁신지구가 기존 추진사업 FACT와 별도로 독립 분석되면서 구 GeoJSON reference 경로를 계속 요구하던 구조 때문이었다.

## 수정
- UQ120 BZ604 자동 매핑 제거
- 구 GeoJSON loader 및 `/api/reference/urban-regeneration-innovation` 제거
- 별도 도시재생 카드/미니맵/NED 조회/용산 대표지번/200m 버퍼 로직 제거
- 김포공항 SHP를 기존 추진사업 FACT에 직접 병합
- 도심공공주택복합 혁신지구 배제는 기존 추진사업 FACT의 SHP 중첩만 사용
- 자율주택의 `도시재생활성화지역` 법정 조건은 혁신지구와 구분
- Retry 13은 rejected-only 1회 순차 재실행으로 정리

## SHP 검증
- component set: SHP/SHX/DBF/PRJ/CPG complete
- CRS: EPSG:5181
- feature: 1 Polygon
- source area: 359,275.76㎡
- WGS84 변환 geometry valid
- 기존 추진사업 공간중첩 엔진 주입 테스트: `도시재생혁신지구` 1건 정상 검출

## 코드 잔존검사
다음 문자열/구조는 최종 코드에서 0건:
- `urban_regeneration_innovation_reference.geojson`
- `/api/reference/urban-regeneration-innovation`
- `urbanRegenerationRestrictionAnalysis`
- `analyzeUrbanRegenerationRestrictions`
- `URBAN_REGEN_REFERENCE_REVIEW_BUFFER_M`
- `URBAN_REGEN_YONGSAN_ANCHORS`
- `ccUrbanRegenerationMini`
- `housing_regeneration_innovation` NED category
- `도시재생 관련 지역·지구` 독립 카드

## 보존 검증
기준 WIP 대비 아래 핵심 함수 byte hash 동일:
- `densityForScheme()`
- `runAllSchemeChecks()`
- `analyzeSchemeStreetBlocks()`
- `analyzeRoadAccess()`
- `runAllAutoAnalyses()`

## Retry 13
- `분석실패 현황 재분석` 표시
- `rejected`만 대상
- `NO_DATA / UNKNOWN / NOT_IMPLEMENTED` 제외
- 15초 대기
- 실패항목별 순차 1회 재호출
- 복구 시 기존 `runAllSchemeChecks()` 1회 재실행
- 반복 while / 병렬 Promise.all 제거

## 결과
`regression_check_urban_regen_integrated_r3.py`: **27/27 PASS**
- Python compile PASS
- browser JavaScript syntax PASS
