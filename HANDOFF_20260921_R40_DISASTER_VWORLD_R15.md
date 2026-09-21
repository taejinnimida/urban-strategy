# R40 R15 인수인계 — 재해 원자료 + VWorld domain/진단 보완

## 기준본
- 입력 기준본: `urban-strategy-v2.5.0-20260921-r40-REGULATORY-WIP-SITE-ANALYSIS-REGULATORY-R14(1).zip`
- 기존 사업판정·가로구역·접도·역세권·지구단위·규제 3패널 로직은 보존한다.

## 1. 산사태위험지도
- 원본 보존: `source_landslide_risk_2026_seoul_11.zip`
- 원자료: `11.tif`, EPSG:5181, 10m × 10m, 등급 1~5, NoData 127
- 메타데이터 처리이력: 2026-08-05, 2026 산불추가 비접경지역 자료의 서울(11) 추출본
- 서버 추가 의존성 없이 동작하도록 원본 격자를 손실 없이 row-RLE로 변환:
  - `landslide_risk_2026_seoul_10m_rle.zlib`
  - `landslide_risk_2026_seoul_10m_meta.json`
- 대상지와 10m 격자를 EPSG:5181에서 정확 교차하여 1~5등급별 중첩면적/대상지 비율을 산출한다.
- 값 127은 절대 `위험 없음`으로 해석하지 않고 NoData/등급 미제공 면적으로 별도 계산한다.
- 자연재해 규제 카드 지도에는 원자료 색상표를 따라 등급별 도형을 구분한다.
- 산사태위험지도 등급은 사업 PASS/FAIL을 직접 변경하지 않는 재해안전 FACT다.

## 2. 자연재해위험개선지구
- 원본 보존: `source_natural_disaster_risk_district_seoul_202609.zip`
- 원자료: `LSMD_CONT_UP201_11_202609`, EPSG:5186, 폴리곤 6건
- 대상지 중첩면적/비율을 원도형 기준으로 계산한다.
- DBF의 `NTFDATE`, `ALIAS`, `REMARK` 공란을 임의 보완하지 않는다.
- 결과는 사용자 제공 2026-09 파일과의 중첩 FACT이며 파일 밖 지정현황 부재까지 확정하지 않는다.
- 기존 VWorld NED 자연재해위험개선지구 필지속성은 보조 FACT로 유지한다.

## 3. VWorld 도시관리계획 502/domain 보완
- 키는 기존과 같이 단일 `VWORLD_API_KEY`를 사용한다.
- 브라우저가 `/api/spatial/planning-layers` 호출 시 `window.location.origin`을 `client_origin`으로 전달한다.
- 서버는 `X-Forwarded-Host` 또는 `Host`와 hostname이 같은 origin만 허용한다.
- 허용된 origin은 VWorld 요청의 `domain`과 `Referer`에 동일하게 적용한다.
- 불일치·잘못된 origin은 기존 `VWORLD_DOMAIN` → `RENDER_EXTERNAL_HOSTNAME` → localhost 폴백으로 되돌린다.
- 실패 결과에 다음 진단값을 보존한다.
  - `route`: direct / vworld_proxy
  - `direct_error`
  - `proxy_status`
  - `domain_sent`
- `/api/spatial/planning-layers` 응답에 `client_domain`, `server_domain`도 포함한다.
- UI 도시계획 GIS 상태문구에 첫 실패의 route/domain/direct/proxy 진단을 표시한다.

## 4. 판정 원칙
- 신규 재해자료는 규제·재해 추가검토 FACT이며 기존 사업방식 PASS/FAIL 판정식을 임의 변경하지 않는다.
- 자료 미확보, 래스터 NoData, 파일 범위 밖은 `없음`으로 확정하지 않는다.
- 산사태 1~5등급의 법적 의미/사업제한은 별도 근거 없이 새로 추정하지 않는다.

## 5. 회귀검사
- 기존 R4~R14 회귀검사 전체 PASS
- 신규 `regression_check_disaster_vworld_r15.py`: 29/29 PASS
- Python compile PASS
- Browser JavaScript syntax PASS
