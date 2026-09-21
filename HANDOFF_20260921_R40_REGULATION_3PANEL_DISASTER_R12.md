# R40 규제지역 3종 분리 + 재해공간자료 R12 인수인계

## 기준

- Parent: `urban-strategy-v2.5.0-20260921-r40-REGULATORY-WIP-LANDPRICE-BUSINESS-COEFFICIENT-R11.zip`
- Child: `urban-strategy-v2.5.0-20260921-r40-REGULATORY-WIP-REGULATION-3PANEL-DISASTER-R12.zip`
- 내부 버전 `v2.5.0` 유지.

## R12 핵심

기존의 하나짜리 `개발제한 및 계획제한 분석`은 삭제했다.

### 1. 자연환경 규제
`natural_park / ecological_landscape / wildlife_special / water_source / river_zone`

### 2. 교통·안보 규제
`railroad_protection / airport_obstacle / military_flight`

### 3. 자연재해 규제
`disaster_risk / disaster_prevention_district / landslide_risk / steep_slope_risk / flood_management / flood_expected / flood_trace_2025`

각 그룹은 독립 카드 + 독립 Leaflet 미니맵으로 표시한다.

## 별도 모듈로 유지

- 지구단위계획: CURRENT PLAN 상세검토
- 도시계획시설: 기존 도시계획시설 카드/도면
- 기존 자연환경분석: 도시자연공원구역·GB·비오톱1등급·공익용산지
- 문화재관련 현황: LT_C_UO301 + 국가유산공간정보 WMS + NED 역사문화환경 보존지역

## 재해 신규 서버 FACT

`POST /api/spatial/disaster-reference-intersections`

서버가 서울 열린데이터광장 공개 ZIP을 필요할 때 최초 다운로드 후 `/tmp` 캐시한다.

- `OA-21172` 풍수해 침수예상도 → 위험예측
- `OA-15636` 2025 침수흔적도 → 과거 발생이력

원자료 오류·접속실패는 UNKNOWN/ERROR이며 NONE으로 확정하지 않는다.

산림청 산사태위험지도는 무료신청 다운로드 방식이므로 R12에 원본을 임의 내장하지 않았다. 현재는 NED `산사태취약지역` 양성정보를 사용하며, 추후 원본 파일을 정식 확보하면 별도 raster/vector 교차모듈로 확장할 수 있다.

## 수정 금지/보존

이번 R12에서 사업방식 RULE, PASS/FAIL/REVIEW, 순위, 밀도, 공공기여, 접도, 가로구역, 사업구역 로직은 변경하지 않았다.

특히 RETRY13(`analysisStepRegistry`, 15초 후 실패단계 1회 재분석, 150초 예산)과 UQ111 용도지역 핵심 FACT 서버 안정화는 그대로 유지한다.

## 검증

- R12 targeted: 47/47 PASS
- disaster loader synthetic unit: PASS
- R4: 30/30 PASS
- R5: 19/19 PASS
- R8: 44/44 PASS
- R10: 22/22 PASS
- R11: 33/33 PASS
- JS syntax: PASS
- app.py compile: PASS

R9 구검사 20/21은 역사적 baseline `app.before_r9.html` 부재 때문에 hash 비교 1개가 수행되지 않은 것으로 기능 실패가 아니다.
