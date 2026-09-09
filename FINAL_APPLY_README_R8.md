# R8 FINAL — 분석 파이프라인 안정화 패치 적용안내

이 ZIP은 **전체 저장소 교체용이 아니라 코드/파생도면 패치**입니다.
기존 저장소의 정상 데이터 파일을 삭제하거나 이 ZIP만으로 저장소 전체를 교체하지 마십시오.

## 덮어쓸 파일
- `app.py`
- `app.html`
- `downtown_characteristic_management_derived.geojson`

## 검증용 파일
- `targeted_regression_checks_r8.py`
- `VERIFICATION_20260909_R8_FINAL.md`

## 최종 설계 결정
1. 필지선택/지도그리기/주소/SHP 입력 모두 `구역계 확정 -> 검토하기 -> 전체 분석` 흐름으로 통일합니다.
2. 필지선택 구역계 갱신 시 선택필지는 보존하고, 이전 구역 기준의 가로구역/역세권/도시계획/도로/의료 등 stale Fact만 초기화합니다.
3. 제도별 가로구역은 후속 역세권/간선가로 판정의 선행 Fact이므로 **가장 먼저 순차 완료**합니다.
4. 다만 `basic_unit_seoul.zip`이 없는 경우 `/health`를 5초 내 확인하여 VWorld/백엔드 가로구역 연산을 시작하지 않고 즉시 `REVIEW`로 처리한 뒤 후속 Fact 분석으로 넘어갑니다.
5. 가로구역을 다른 대형 공간조회와 백그라운드 병렬화하지 않습니다. 기존 코드의 저사양 Render 인스턴스 안정성 원칙(대형 SHP/외부 API 순차 실행)을 유지합니다.
6. `/health`는 도로 번들뿐 아니라 핵심 기준자료 설치상태를 `reference_data`, `reference_data_missing`, `analysis_reference_ready`로 노출합니다.
7. 도로 ZIP 단순 탐색 함수는 캐시하지 않고, 실제 SHP 구성 확인/추출 함수 `_road_shape_zip_cache_dir()`에만 `@lru_cache(maxsize=1)`를 유지합니다.

## 반드시 보존할 기존 데이터
기존 정상 저장소에 있던 다음 자료를 삭제하지 마십시오.
- `road_shp_seoul.zip`
- `stations.json`
- `centers.json`
- `station_entrances.json`
- `uq181_legal.zip`
- `uq120_project.zip`
- `safe_medical_reference.json`
- 비오톱/공익용산지/학교보호구역 기준자료
- `basic_unit_seoul.zip` (가로구역 자동산정에 필요)

`basic_unit_seoul.zip`이 없으면 가로구역만 `REVIEW`이며, 토지/건축/도시계획 등 다른 분석은 계속합니다.
