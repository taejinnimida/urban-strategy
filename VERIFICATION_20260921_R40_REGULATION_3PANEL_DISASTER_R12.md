# R40 규제지역 3종 분리 + 재해공간자료 R12 검증보고서

## 1. 기준본

- 유일 기준본: `urban-strategy-v2.5.0-20260921-r40-REGULATORY-WIP-LANDPRICE-BUSINESS-COEFFICIENT-R11.zip`
- 내부 버전: `v2.5.0` 유지
- 작업 방식: R11 전체를 이어서 최소 수정. 사업판정 RULE은 변경하지 않음.

## 2. 사용자 확정 요구사항

기존 단일 `개발제한 및 계획제한 분석`을 삭제하고 규제성격별로 다음 3개 독립 분석창/도면으로 재편한다.

1. **자연환경 규제**
   - 자연공원구역
   - 생태·경관보전지역
   - 야생생물 특별보호구역
   - 상수원보호구역
   - 하천구역
2. **교통·안보 규제**
   - 철도보호지구
   - 공항 장애물 제한표면
   - 군사시설보호·비행안전·대공방어
3. **자연재해 규제**
   - 자연재해위험개선지구
   - 방재지구
   - 산사태취약지역
   - 급경사지 붕괴위험지역
   - 홍수관리구역
   - 서울시 풍수해 침수예상도
   - 서울시 침수흔적도(2025)

별도 전용검토가 이미 있는 항목은 중복 제거한다.

- 지구단위계획구역 → 기존 CURRENT PLAN 상세검토 유지
- 도시계획시설 → 기존 도시계획시설 분석 유지
- 역사문화환경 보존지역 → 기존 `문화재관련 현황`으로 통합
- 기존 `자연환경분석`(도시자연공원구역·GB·비오톱1등급·공익용산지)은 별도 카드로 유지

## 3. 구현 내용

### app.html

- 기존 `siteDetail_regulatoryConstraints` 삭제.
- 다음 3개 신규 카드/미니맵 추가.
  - `siteDetail_naturalRegulations` / `ccNaturalRegulationMiniMap`
  - `siteDetail_transportSecurityRegulations` / `ccTransportSecurityRegulationMiniMap`
  - `siteDetail_disasterRegulations` / `ccDisasterRegulationMiniMap`
- `문화재관련 현황`에 `역사문화환경 보존지역` 상태/도형을 추가.
- 규제도면은 항목별 FACT를 같은 그룹 도면에 합성하고 범례/툴팁으로 구분.
- 기존 `개발제한·계획제한 추가검토` 문구를 `규제지역 추가검토`로 정리.
- 보고서의 규제 도면에 `DISASTER` 분류 색상 추가.

### app.py

- NED 규제명 분류에 다음 재해항목을 보강.
  - `landslide_risk`
  - `steep_slope_risk`
  - `flood_management`
- 신규 서버 엔드포인트:
  - `POST /api/spatial/disaster-reference-intersections`
- 서울 열린데이터광장 공개 ZIP을 서버에서 필요 시 내려받아 `/tmp/urban_strategy_disaster`에 캐시하고 대상지와 실제 교차 계산.
  - 서울시 풍수해 침수예상도
  - 서울시 2025년 침수흔적도
- 침수예상도와 침수흔적도를 각각 `위험예측 FACT` / `과거 발생이력 FACT`로 분리.
- 외부자료 조회 실패/ZIP 형식 변경 시 `known=false / ERROR`로 유지하여 비해당으로 오판하지 않음.

## 4. 공식 재해자료 확인

### 서울시 풍수해 침수예상도
- 서울 열린데이터광장 `OA-21172`
- 좌표계 EPSG:5186 / 인코딩 windows-949
- 공개 파일: `침수예상도.zip`
- 데이터셋: https://data.seoul.go.kr/dataList/OA-21172/A/1/datasetView.do

### 서울시 침수흔적도
- 서울 열린데이터광장 `OA-15636`
- 최신 공개목록에 `2025년 서울시 침수흔적도.zip` 존재
- 데이터셋: https://data.seoul.go.kr/dataList/OA-15636/F/1/datasetView.do

### 산사태위험지도
- 산림청/공공데이터포털의 공식 산사태위험지도는 10m×10m 격자 위험등급 자료이나, 원자료 내려받기는 산림공간정보서비스의 무료신청 절차를 거치는 방식이다.
- 따라서 이번 R12에서는 이 자료를 임의 다운로드/내장하지 않고, NED의 `산사태취약지역` 양성정보만 자동 FACT로 사용한다.
- 원자료: https://www.data.go.kr/data/15074817/fileData.do

## 5. 판정 안전장치

- 전용 원도형이 없는 NED 항목은 **양성만 확정**한다.
- NED에서 항목이 보이지 않았다는 이유만으로 `비해당`을 확정하지 않는다.
- 재해 공식 ZIP 다운로드 실패도 `비해당`으로 바꾸지 않는다.
- 규제 존재는 기존 사업방식 PASS/FAIL/REVIEW를 직접 변경하지 않고 `추가검토 FACT`로 유지한다.

## 6. 보존 확인

R11 대비 다음 핵심 JS 함수는 byte-identical 검증.

- `densityForScheme()`
- `runAllSchemeChecks()`
- `checkActivationFromFacts()`
- `analyzeSchemeStreetBlocks()`
- `analyzeRoadAccess()`
- `buildSiteFactStore()`

또한 다음 기능을 유지함.

- RETRY13 분석실패 1회 재분석
- UQ111 용도지역 핵심 FACT server proxy/cache/fallback
- CURRENT PLAN 지구단위계획 상세검토
- 도시계획시설 전용 분석
- 국가유산보호구역 + 국가유산공간정보 WMS
- 접도·가로구역·사업구역 및 기존 사업판정

## 7. 검증 결과

- 신규 `regression_check_regulation_3panel_disaster_r12.py`: **47/47 PASS**
- 신규 `regression_check_disaster_loader_r12.py`: **PASS** (합성 SHP ZIP의 CRS 변환·bbox 조회·교차면적 계산)
- R4 activation/prior competition: **30/30 PASS**
- R5 analysis stability: **19/19 PASS**
- R8 semi-industrial popup: **44/44 PASS**
- R10 unitplan/modal/arterial UI: **22/22 PASS**
- R11 land-price/business coefficient: **33/33 PASS**
- `node --check` inline JavaScript: **PASS**
- `python -m py_compile app.py`: **PASS**
- R9 구검사는 **20/21 PASS**. 실패 1건은 현재 기준 ZIP에 역사적 비교파일 `app.before_r9.html`이 존재하지 않아 baseline hash 비교를 실행할 수 없는 환경조건이며, R9 기능 자체의 실패가 아님.

## 8. 런타임 확인 필요

배포 후 실제 서울 대상지에서 아래만 확인한다.

1. 자연환경/교통·안보/자연재해 3개 카드가 각각 1개 도면으로 표시되는지.
2. 문화재관련 현황에 역사문화환경 보존지역이 함께 표시되는지.
3. 서울시 침수예상도/침수흔적도 최초 요청 시 서버 다운로드 후 중첩면적이 표시되는지.
4. 외부 서울 데이터 서버 장애 시 ERROR/미확인으로 남고 `중첩 없음`으로 바뀌지 않는지.
5. 기존 지구단위 CURRENT PLAN·도시계획시설·사업판정 결과가 R11과 동일하게 유지되는지.
