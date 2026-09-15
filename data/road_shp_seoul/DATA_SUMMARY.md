# 첨부 데이터 요약 (검토용, 코드 미포함)

## 파일: road_shp_seoul/ (도로도형_전체분_서울특별시.zip 중 11000 폴더)
좌표계: GRS80 TM (PROJCS ITRF2000_TM, central meridian 127.5, false easting 1,000,000 / northing 2,000,000)

| 레이어 | 형태 | 서울 전체 건수 | 주요 속성 |
|---|---|---|---|
| TL_SPRD_MANAGE | LineString(도로중심선) | 66,688 | ROAD_BT(폭원,m), RN(도로명), ROA_CLS_SE(도로등급), RDS_MAN_NO(도로관리번호), SIG_CD |
| TL_SPRD_RW | Polygon(도로 경계=실폭도로) | 60,534 | RW_SN, SIG_CD, OPERT_DE (도로명·폭원 속성 없음) |
| TL_SPRD_INTRVL | LineString(기초구간) | 749,521 | 주소 간격표시용, 접도분석과 무관 |

## 확인된 사실
- TL_SPRD_RW는 실제 도로 면적 형상(실폭도로) 폴리곤이며, 중심선 버퍼링 근사가 아니다.
- TL_SPRD_RW에는 도로명·폭원 속성이 없어, 폭원 판정을 하려면 TL_SPRD_MANAGE와 공간매칭(최근접 등)이 필요하다.
- 인코딩은 cp949(EUC-KR)이며 utf-8로 읽으면 필드명 디코딩 오류가 난다.
- 샘플 검증(종로구 성균관로13길 인근)에서 실제 폴리곤-중심선 매칭 및 임의 도형 접도 계산은 기술적으로 성공했으나, **접도율 산정 기준이 지적(연속지적) 필지 경계가 아니라 임의 사각형이어서 법적으로 무효한 데모였음.**
- app.py에는 이미 VWorld 연속지적도 API(`LP_PA_CBND_BUBUN`) 연동 함수(`_vworld_parcel_at_point`, `_vworld_parcel_by_address`)가 존재하나, 본 분석 환경은 VWorld 도메인에 대한 네트워크 egress가 차단되어 있어 실제 API 응답으로 검증하지 못했다.
