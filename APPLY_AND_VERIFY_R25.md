# R25 LOCAL-FIRST / FINAL-FACT / EXTERNAL-SUPPLEMENT 적용·검증

기준본: R24 (`urban-strategy-v2.5.0-20260923-r40-R24-BOUNDARY-TOPOLOGY-DXF-FIX`)

## 교체 파일
GitHub 루트의 다음 2개를 교체한다.
- `app.py`
- `app.html`

R23의 도시관리계획 server-first → browser fallback과 R24의 필지 위상보정/DXF 수정은 유지한다.

## R25 변경범위
1. 토지대장 server-first → VWorld browser JSONP fallback
   - Render/VWorld transport failure 때 브라우저 `ladfrlList` → `getLandCharacteristics` 순으로 재시도
   - 서버의 정상 empty 응답은 중복 조회하지 않음
   - 성공 시 route=`vworld_jsonp_browser_fallback`

2. 상태창 `보유자료 / 외부보강자료` 분리
   - 규제/재해 카드 및 진행상태에서 local bundled FACT와 WMS/VWorld/원격자료를 구분

3. 최종 FACT 기준 집계
   - 자연환경 4종 등은 과거 요청 오류를 누적하지 않고 최종 known/present를 기준으로 상태 결정
   - 토지 상태에는 공식면적 확보수, browser 보완수, 실패 PNU를 명시

4. 서울 침수예상도·침수흔적도 URL 재검증
   - 공식 데이터셋 landing page는 현재 유지
     - 침수예상도: https://data.seoul.go.kr/dataList/OA-21172/A/1/datasetView.do
     - 침수흔적도: https://data.seoul.go.kr/dataList/OA-15636/F/1/datasetView.do
   - 기존 코드의 고정 `nio_download.do?...seq=...` 직접주소는 제거
   - 검증된 직접 ZIP URL이 있을 때만 환경변수로 설정:
     - `SEOUL_FLOOD_EXPECTED_URL`
     - `SEOUL_FLOOD_TRACE_2025_URL`
   - 미설정 시 LOCAL 재해자료는 정상 판정하고, 이 두 자료만 `external_source_unavailable`로 분리

5. ECVAM endpoint/API 진단
   - 공식 `apiConfirm.do?APIKEY=...` bootstrap 검증
   - bootstrap script에서 WMS endpoint 발견 시 사용
   - `/api/reference/ecvam-status?probe=true`에서 실제 64x64 `nem_law_01` GetMap까지 검사
   - API key는 응답/오류에서 마스킹

6. 규제 중첩 도면
   - 원도형(context/source zone) + 실제 intersection(overlap) geometry 동시 표시
   - 원도형은 옅고 점선, 실제 중첩은 진한 스타일
   - `확인완료 · 중첩 없음` 항목도 숨기지 않고 표시

7. 의료시설 LOCAL-FIRST
   - packaged TbHospitalInfo/whitelist 후보 → bundled 2020-12 cadastral snapshot 대표필지 우선 → 350m 분석
   - snapshot 사용 시 기준일/최신 분할·합병 한계를 명시

8. 의료시설 VWorld browser fallback
   - 서버/로컬에서 대표필지를 확정하지 못한 후보만 브라우저 연속지적 조회
   - 성공 시 `VWORLD_BROWSER_CADASTRAL_FALLBACK`으로 출처 표시 후 350m 재계산

## 배포 확인
`/health`에서 다음을 확인한다.

```json
"planning_browser_fallback_patch_marker":"R23_SERVER_FIRST_BROWSER_FALLBACK_20260923",
"external_circuit_patch_marker":"R22_VWORLD_GLOBAL_CIRCUIT_20260922",
"local_first_fact_status_patch_marker":"R25_LOCAL_FIRST_FACT_STATUS_20260923",
"land_ledger_browser_fallback":true,
"safe_medical_local_first":true
```

### 토지대장
Render 로그에 VWorld 502/ConnectionError가 있어도 브라우저 VWorld가 정상이라면 진행창 토지 항목에서
- `공식면적 X/Y`
- `브라우저 보완 N필지`
가 표시되어야 한다.

둘 다 실패한 PNU만 REVIEW로 남아야 한다.

### 규제·재해
- 보유자료의 최종 FACT가 모두 확인되면 과거 transient 오류가 진행창을 계속 노란색으로 만들지 않아야 한다.
- 외부 WMS/원격자료 오류는 `외부보강`으로 별도 표시한다.
- 규제 중첩 시 미니맵에 원도형과 실제 중첩영역이 함께 표시되어야 한다.

### 의료시설
- 보유 snapshot에서 대표필지가 확인되면 VWorld 장애와 관계없이 350m FACT가 생성되어야 한다.
- snapshot으로 못 찾은 후보만 브라우저 VWorld fallback을 시도한다.

### ECVAM
`/api/reference/ecvam-status?probe=true` 확인:
- `probe.bootstrap = "OK"`
- `probe.wms.ok = true` 이면 실제 GetMap까지 성공
- false면 HTTP status/content-type/error를 기준으로 외부 WMS 문제를 진단

### 침수자료
현재 서울 공식 데이터셋은 유지되지만 직접-download 주소는 동적으로 바뀔 수 있으므로 하드코딩하지 않는다.
환경변수 미설정은 플랫폼 오류가 아니라 `외부보강자료 미설정/미확보` 상태이며 LOCAL 재해 FACT에는 영향을 주지 않는다.

## 검증 결과
- `python -m pytest -q tests/test_pipeline_repair.py` → 25 passed
- `node tests/pipeline_order.test.cjs` → 3 tests PASS
- `python -m py_compile app.py` → PASS
- 현재 `app.html`의 inline script 5개 추출 후 `node --check` → PASS
- R24 DXF 회귀: ezdxf audit errors=0 / fixes=0
