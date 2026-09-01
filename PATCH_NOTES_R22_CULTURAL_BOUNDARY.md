# R22 boundary + long-term-jeonse characteristic-area fix

## 1. Boundary bug fixed
- User-drawn / address / SHP boundary remains the authoritative analysis geometry.
- Automatic cadastral parcel selection no longer overwrites the boundary with a parcel union.
- Parcel union is retained only as a reference statistic.
- The boundary changes to parcel lines only when the user explicitly selects **선택필지로 구역계 갱신**.

## 2. Hill layer
- No public official hill SHP file was confirmed.
- Hill automatic analysis is disabled and remains REVIEW/manual check.
- No DEM reconstruction is treated as an official hill polygon.

## 3. Verified official spatial datasets for long-term-jeonse characteristic-area review
- Seoul zoning SHP: `UQ111_용도지역(도시지역)_202602.zip` (EPSG:5174). Used to identify 제1종전용, 제2종전용, 제1종일반.
- Seoul landscape district SHP: `UQ121_용도지구(경관지구)_202602.zip` (EPSG:5174). Used to identify 역사문화특화경관지구 where the district name is present.
- Seoul heritage-designation area SHP: `CUL211_문화재지정구역_202602.zip` (EPSG:5174). Official source located; not bundled in this ZIP.
- Seoul heritage-designation linear SHP: `CUL210_문화재지정구역(선형)_202602.zip` (EPSG:5174). Official source located; not bundled in this ZIP.
- National heritage spatial service: designated heritage + alteration-permission standard datasets are distributed as SHP by 국가유산청.
- Live cross-check layer added: VWorld `LT_C_UO301` (국가유산보호도/보호구역).

## 4. Long-term-jeonse rule evidence
The rule now separately reports:
- 전용·제1종 일반주거지역
- 역사문화특화경관지구
- 국가유산보호구역
- 구릉지: 공개 SHP 미확보 / 수동검토

The combined exclusion remains REVIEW because the operating standard allows committee judgment and because hill/other place-character boundaries are not completely automated.
