# 2025 세분류 토지피복도 — 도엽별 공간색인 파일

## 실제 변환 결과

- 원자료: 사용자 제공 `토지피복도.zip`(137개 내부 도엽 ZIP)
- 변환 방식: 137개 GeoPackage `.gpkg`, 각 도엽의 `rtree_landcover_geom` 공간색인
- 도엽 선택: `tile_index.sqlite`의 `tile_rtree` 공간색인 (EPSG:4326 최소외접범위)
- 원본 컬럼: `L1_CODE/L1_NAME`, `L2_CODE/L2_NAME`, `L3_CODE/L3_NAME` 등 전부 보존
- 원본 CRS: 각 SHP의 원본 `prj`를 GeoPackage와 도엽 색인에 보존 (ITRF2000 TM 계열)
- 출처 검증: `source_fingerprint.json`, `tile_index.sqlite` 도엽별 ZIP SHA256
- 공간정보 원본(도형·속성)은 변환 외 임의 정리하지 않음. 조회 시 원본 위상이 불량하면 `make_valid` 처리를 기록하며 임의로 완전한 자료라고 표시하지 않음.
- 제공 범위: 서울·경기 **전역이 아님**. `manifest.json`의 좌표 사각형은 전체 수록범위가 아니라 외접범위임.

## 디렉터리

```
landcover_2025_indexed/
  manifest.json
  source_fingerprint.json
  tile_index.sqlite              # RTree: 대상지에 겹칠 가능성이 있는 도엽 검색
  tiles/
    37608030.gpkg                # RTree: 도엽 안의 개별 폴리곤 검색
    ...
    37709024.gpkg
  sample_middle_seoul.json       # 실증: 서울시 일부 예시 구역, 면적표
```

## 서버에서 사용하는 방법

`query_landcover_gpkg.py`는 기존 `shapely`, `pyproj` 및 Python 내장 `sqlite3`만 사용한다.
컨버터와 달리 서버에 GDAL/pyogrio를 추가할 필요가 없다.

```python
from query_landcover_gpkg import query
site_geojson = {"type": "Polygon", "coordinates": [[...]]}  # WGS84 경도,위도
result = query('/app/national_data/landcover_2025_indexed', site_geojson,
               include_features=True)
```

`result['groups']['L1'/'L2'/'L3']`은 구역계 전체면적을 분모로 한 면적표이며,
`result['features']`는 원본 L1/L2/L3 코드가 보존된 EPSG:4326 GeoJSON 도형이다.
도형은 동일한 FACT로 SVG/웹 지도, Excel, DXF에 공급할 수 있다.

`COMPLETE_GEOMETRY`: 조회한 구역계 면적이 공간도형으로 덮이고,
분류 간 중첩이나 임의 위상보정이 검출되지 않은 경우에 한함.
`PARTIAL`: 원자료의 위상보정, 도형 누락, 미중첩 등 확인 필요.
`NOT_COVERED`: 데이터가 없는 범위 — **토지피복 0이라는 뜻이 아님.**

## 추가 도엽 구축

원래 ZIP 형식 그대로 `convert_landcover_to_gpkg.py --input 새_압축.zip --output 새_버전_폴더`로 실행.
현재 도엽 색인에는 최초 업로드 ZIP의 SHA256이 기록된다. 새로운 원자료는 반드시
새 버전별 출력 폴더로 관리하고, 중복 도엽과 원자료 갱신 여부를 검증하여 병합한다.

## 운영 참고

1. Render/Cloudtype에 680MB GPKG를 두 벌씩 포함하면 빌드 크기가 커지고 배포가 느려질 수 있다.
2. 원본 저장소의 이용조건, 외부 재배포 및 유료 서비스 활용 조건을 확인한다.
3. 정적 파일 서비스 또는 공동 저장소는 실제 배포 환경의 읽기 권한·지속성·트래픽 조건을 검증해야 한다.
4. **이 ZIP만으로 기존 웹 애플리케이션에 자동 배포되지는 않는다.** 조회 모듈을 서버 라우터에 연결하고 데이터를 서버에 배치해야 한다.