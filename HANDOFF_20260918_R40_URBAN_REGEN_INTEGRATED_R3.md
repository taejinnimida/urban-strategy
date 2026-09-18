# R40 도시재생 공간자료 재정리 R3

## 목적
기존 UQ120/BZ604 `도시재생활성화지역` 자동공간자료와 별도 도시재생혁신지구 NED/GeoJSON 검토모듈을 제거하고, 사용자 확정 DXF에서 변환한 김포공항 도시재생혁신지구 SHP를 기존 추진사업 FACT 흐름에 직접 편입한다.

## 최종 구조
1. UQ120 `BZ604` 매핑 제거.
2. `urban_regeneration_innovation_gimpo.shp`(EPSG:5181)를 서버에서 읽어 WGS84로 변환.
3. SHP 1건을 기존 추진사업 project registry에 `도시재생혁신지구`로 병합.
4. 대상지 중첩면적/중첩률은 기존 project registry 공간연산을 그대로 사용.
5. 기존 독립 도시재생 모듈(별도 카드/미니맵/NED/GeoJSON/용산 대표지번/200m 버퍼/reference endpoint)은 제거.
6. 도심공공주택복합의 도시재생혁신지구 배제 FACT는 project registry의 SHP 중첩결과만 읽는다.
7. 자율주택정비의 법정 `도시재생활성화지역` 조건과 `도시재생혁신지구`는 의미가 다르므로 혼동하지 않도록 정규식을 분리한다.
8. 13번 실패재분석은 기존 전체분석 종료 후 rejected만 15초 대기 후 1회 순차 재실행한다.

## 기준 SHP
- 파일: `urban_regeneration_innovation_gimpo.*`
- 원본: 사용자 제공 `도시재생혁신지구.dxf`
- CRS: EPSG:5181
- geometry: Polygon 1건
- 면적: 359,275.76㎡

## 주의
이 산출물은 `REGULATORY-WIP-CHECKPOINT` 계열에 변경사항을 적용한 병합 후보본이다. canonical `INNOVATION-2TYPE-POPUP3-ONLY` 원본 바이트가 현 세션에서 materialize되지 않아 canonical 승격본으로 표기하지 않는다.
