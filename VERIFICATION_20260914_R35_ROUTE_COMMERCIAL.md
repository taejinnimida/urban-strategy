# VERIFICATION 20260914 R35 — 노선형 상업지역 판정모형 참조도형

## 기준본
- 화면/판정 기준: `urban-strategy-v2.5.0-20260914-semiindustrial-card-title-ui-r34-PATCH.zip`
- 백엔드: R32 이후 R33/R34에서 `app.py` 변경이 없으므로 R32 `app.py`를 기준으로 R35 참조자료 API만 추가
- 앱 내부 버전: **v2.5.0 유지**

## 사용자 제공 원자료
- `노선상업 재검토.dxf`
- 용도: **정비사업플랫폼 내부 판정모형에 한정한 노선형 상업지역 참조도형**
- 공식 서울시 법정도형으로 취급하지 않음
- 일반 용도지역 `LT_C_UQ111` FACT를 대체하지 않음

## 원자료 구조 확인
- DXF layer: `SEOUL_LINEAR_COMMERCIAL_CA`
- modelspace: `POLYLINE 1,672개 + INSERT 1,700개`
- 폐합 POLYLINE 1,672개 중 유효/보정 후 polygon 1,664개
- 중복·접촉 도형 dissolve 후 **829개 polygon component**
- 원좌표 면적 합계: 약 **545,009.915㎡**
- 원좌표 범위: `190699.355, 440423.867 ~ 205432.008, 456247.137`

## 좌표계
- 판정모형 기준 원좌표계: **EPSG:5174**
- 브라우저용 참조자료: **EPSG:4326 GeoJSON**으로 변환
- 출력 GeoJSON 위치범위가 서울 WGS84 범위에 들어오는지 회귀검사 포함
- 자료 자체에는 법정 원본 CRS 메타데이터가 없으므로 `MODEL_REFERENCE` 성격을 명시하고, 법정도형으로 승격하지 않음

## 생성 자료
- `route_commercial_reference.geojson`
- top-level metadata:
  - `source_type = MODEL_REFERENCE`
  - `legal_source = false`
  - `reference_name = 노선형 상업지역 판정용 참조도형`
  - `model_use = 역세권활성화 간선가로형 검토 전용`
  - `source_crs = EPSG:5174`
  - `output_crs = EPSG:4326`

## R35 코드 변경
### app.py
1. `ROUTE_COMMERCIAL_REFERENCE_PATH` 추가
2. `_route_commercial_reference_data()` 추가
3. `GET /api/reference/route-commercial` 추가
4. `/health`의 `reference_data`에 `route_commercial_model` 설치상태 추가
5. 참조자료가 없으면 빈 FeatureCollection + `available:false`를 반환하여 **REVIEW** 경로 유지

### app.html
1. `analyzeActivationArterial()`의 주 판정자료를 변경
   - 종전: `LT_C_UQ111` 상업지역 + 8~18m 띠형 형상 추정 + 도로 인접
   - R35: `MODEL_REFERENCE` 노선상업 참조도형 + 대상지/가로구역 공간관계
2. 종전 8~18m/장단비 형상추정 helper는 **LEGACY DIAGNOSTIC ONLY**로 남기고 주 판정에서 호출하지 않음
3. 도로자료는 지도·문맥 표시용으로만 유지하며, 도로조회 실패가 노선상업 FACT 자체를 실패시키지 않음
4. 역세권활성화 전용 가로구역이 아직 계산되지 않았으면 `FAIL`로 선판정하지 않고 **REVIEW** 유지
5. 가로구역 계산 완료 후 전체 참조도형 cache와 다시 중첩하여 최종 PASS/FAIL 판정
6. UI에 `MODEL_REFERENCE · 내부 판정모형`, `공식 법정도형 아님`을 명시
7. 다른 사업의 용도지역·입지 판정에는 이 자료를 사용하지 않음

## 판정상태 원칙
- 참조도형 미설치/조회실패 → **REVIEW**
- 참조도형 확보 + 가로구역 미산정 → **REVIEW**
- 대상지 자체 참조도형 중첩 → **PASS**
- 역세권활성화 전용 가로구역과 참조도형 중첩 → **PASS**
- 참조도형 확보 + 가로구역까지 확정 + 중첩 없음 → **FAIL**

## 검증
`targeted_regression_checks_r35.py`
- **33/33 PASS**
- GeoJSON source_type/legal_source/CRS/feature count/geometry validity 확인
- `analyzeActivationArterial()`에서 `LT_C_UQ111` 직접조회 제거 확인
- 형상추정 classifier 주 판정 미사용 확인
- REVIEW/PASS/FAIL state machine 실행검사
- 가로구역 후처리가 전체 reference cache를 다시 읽는지 확인
- R34 준공업 사업카드 UI 유지 확인
- `node --check` PASS
- `python -m py_compile app.py` PASS

### 이전 회귀스크립트 비교
R34와 R35에 기존 r15~r32 스크립트를 동일 실행하여 비교했다.
- 대부분 동일 결과 유지
- R25/R27/R32의 기존 실패는 **R34에서도 동일하게 존재한 선행 실패**
- R18의 `roadCount===0`, `zoneCount===0` REVIEW 2개 검사는 R35에서 의도적으로 폐기된 구 `LT_C_UQ111 + 도로` 추정모형 전용 검사이므로 R35 신규 회귀검사로 대체함

## 배포 후 확인
1. 역세권활성화 간선가로형 대상지에서 주황 참조도형이 실제 위치에 맞게 표시되는지
2. 대상지가 참조도형에 직접 걸린 경우 PASS가 되는지
3. 대상지는 직접 안 걸리지만 역세권활성화 전용 가로구역이 참조도형을 포함하면 PASS가 되는지
4. 가로구역 계산 전에는 REVIEW가 유지되는지
5. 다른 사업의 용도지역/사업판정이 변하지 않는지
