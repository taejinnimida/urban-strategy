# v2.5.0 R6-FIX2 검증기록 — 필지선택 구역계 경로 통일 + /health NameError

기준본: `urban-strategy-v2_5_0-20260909-activation-streetblock-r6.zip` (R6, 사용자 업로드)
직전 패치: R6-FIX1 (버튼 활성화만 추가 — 아래 사유로 불완전 판정, 이 문서로 대체)

## 1. 필지선택 구역계 경로가 나머지 3개 입력경로와 구조가 달랐던 문제

### 원인
`app.html`의 구역계 입력경로는 4개(①지도 그리기, ②주소필지 적용, ③SHP 업로드, ④필지선택)인데
①~③은 전부 `measureAndSync()`를 호출해 (a) 이전 구역계 Fact 초기화 (b) 검토하기 버튼 활성화
(c) 사용자가 '검토하기'를 눌러야 무거운 공식조회가 도는 지연실행 구조를 공유하고 있었다.

④ 필지선택(`applySelectedParcelsAsBoundary()`, "선택필지로 구역계 갱신"/"선택필지로 구역계 정리" 두 버튼이 공유)만
- `measureAndSync()` 대신 `updateMeasurementOnly()`만 호출 → **검토하기 버튼이 계속 disabled로 남음**
- `analyzeLandLedger/analyzeBuildings/analyzeBuildingHub/analyzeRoadAccess/analyzeSafeMedicalReference`를
  구역계 확정 즉시 자체적으로 실행 → 나머지 경로의 "구역계 확정 → 검토하기 클릭 → 전체 실행" 구조와 불일치,
  가로구역·역세권·도시계획·정비구역 등 나머지 판정은 돌지 않는 중간상태로 남음
- 이전 필지선택으로 이미 분석을 한번 실행한 뒤 필지를 추가/제외해 구역계를 다시 잡아도, 이전 가로구역/역세권/
  도시계획 Fact가 초기화되지 않고 남아있을 수 있었음 (`clearBoundaryAnalysisForNewGeometry()`를 못 쓰는 이유가
  그 함수 내부의 `parcelFeatureMap.clear(); selectedParcelPnus.clear();`가 사용자의 필지선택 자체를 지워버리기 때문)

### 수정
1. `clearBoundaryAnalysisForNewGeometry(options={})`에 `keepParcelSelection` 옵션 추가.
   `true`면 `parcelFeatureMap`/`selectedParcelPnus`는 보존하고 나머지(가로구역·역세권·도시계획·정비구역·
   개발구역·의료시설·schemeResults 등)는 기존과 동일하게 초기화한다.
2. `applySelectedParcelsAsBoundary()`를 나머지 3개 경로와 동일한 구조로 재작성:
   - 병합 geometry 확정 직후 `clearBoundaryAnalysisForNewGeometry({keepParcelSelection:true})` 호출 →
     이전 Fact는 초기화하되 필지선택은 유지
   - 구역계 확정 시점의 즉시 공식조회(토지대장/건축물/건축HUB/도로/의료시설) 호출 제거
   - `updateMeasurementOnly()` → `recalcSelectedParcelStats()`(총 필지수 복원 + `runAllSchemeChecks()`) 순으로 실행
   - `runBtn.disabled=false`, `setSiteReviewStatus(..., 'ready')`로 검토하기 버튼 활성화
   - 나머지 3경로와 동일하게 `updateCompactInfoRail/refreshCompactMiniMaps/syncServiceOverview` 갱신

### 검증
- `node --check`(inline JS 추출): PASS
- 코드 검토: ①②③ 경로(`map.on(L.Draw.Event.CREATED/EDITED,...)`, `applyAddressPreviewAsBoundary()`,
  `loadBoundaryShp()`)는 이번 변경으로 건드리지 않았고, 전부 `measureAndSync()` → 내부적으로
  `clearBoundaryAnalysisForNewGeometry()`(옵션 없음 = 기존과 동일하게 필지선택도 초기화)를 그대로 호출함을 재확인.

## 2. `/health` NameError — 배포 헬스체크 실패 가능성

### 원인
`app.py` `/health` 핸들러가 `"road_bundled_configured": bool(_road_zip_path())`를 참조하지만
R6 `app.py`에는 `_road_zip_path` 함수 정의가 존재하지 않았다. `python -m py_compile`은 함수 본문의
이름 해석을 실행 시점까지 검사하지 않으므로 컴파일은 통과하지만, 실제로 `/health`를 호출하면
`NameError`가 발생한다. Render의 `healthCheckPath`가 `/health`이므로 배포 인스턴스가 비정상으로
판정되어 재시작을 반복할 가능성이 있다.

### 실제 재현 (R6 원본, 수정 전)
```
$ python3 -c "import app; app.health()"
NameError: name '_road_zip_path' is not defined
```

### 수정
`_road_shape_zip_cache_dir()` 내부에 있던 후보경로 탐색 로직을 `_road_zip_candidates()`로 분리하고,
`_road_zip_path()`를 새로 정의해 `/health`가 실제로 참조 가능하게 했다. 기존 `_road_shape_zip_cache_dir()`는
동일한 후보 목록을 `_road_zip_path()` 경유로 사용하도록 변경(동작 동일, 로직 중복 제거).

### 검증 (수정 후)
```
$ python3 -c "import app; print(app.health()['road_bundled_configured'])"
True
```
- `python -m py_compile app.py`: PASS

## 3. 이 R6 ZIP 자체에 빠져 있는 참조 데이터 (코드 수정 대상 아님 — 배포 데이터 점검 필요)

`app.py`가 경로를 참조하지만 이번 ZIP에는 들어있지 않은 파일들 (grep 기준, 후보 파일명 그룹핑):

| 그룹 | 후보 파일명 | 용도(코드 주석/문자열 기준) |
|---|---|---|
| SGIS 기초단위구 | `basic_unit_seoul.zip` / `sgis_basic_unit_seoul.zip` / `basic_unit_2025_seoul.zip` | 가로구역 엔진 seed (R6 문서에 이미 명시된 known issue) |
| 구릉지 | `hill_seoul.zip` / `seoul_hill.zip` / `seoul_hillside.zip` / `hillside_seoul.zip` (`SEOUL_HILL_SHP_PATH` env로도 지정 가능) | 도시·주거환경정비기본계획/생활권계획 구릉지 판정 (해발고도 40m 이상 AND 경사도 10도 이상) |
| 비오톱 | `biotope_seoul.zip` | 비오톱1등급 중첩 판정 |
| 산지구분도 | `forest_classification_seoul_202608.zip` | 공익용산지 중첩 판정 |
| 학교절대보호구역 | `school_protection_seoul_202608.zip` | UOA110 중첩 판정 |
| 정비구역 법정 | `uq181_legal.zip` | 정비사업 GIS 중첩 (기존 `regression_checks.py` 실패 사유로도 이미 언급됨) |
| 개발구역 | `uq120_project.zip` | 개발사업 GIS 중첩 |
| 역 기준자료 | `stations.json`, `station_entrances.json` | 역세권 판정 |
| 중심지 기준자료 | `centers.json` | 지구중심 등 입지유형 판정 |
| 의료시설 | `safe_medical_reference.json`, `safe_medical_parcels_202012.geojson` | 안심주택 의료시설 경로 판정 |

이 파일들이 이미 Render 서비스에 이전 배포분으로 남아있는지, 아니면 이번 ZIP이 패치 작업분만 담고
있어 실제로는 빠진 상태인지는 배포 서버를 직접 확인해야 정확히 알 수 있다 — 이 부분은 코드로 판단할
수 있는 범위를 넘어서므로 임의로 "정상" 또는 "고장"이라 단정하지 않는다.

## 수정범위
- `app.html`: `clearBoundaryAnalysisForNewGeometry()`에 `keepParcelSelection` 옵션 추가,
  `applySelectedParcelsAsBoundary()` 재작성 (버튼 활성화 누락 + 무거운 분석 선행 실행 + stale Fact 미초기화 3가지 동시 수정)
- `app.py`: `_road_zip_path()`/`_road_zip_candidates()` 추가, `_road_shape_zip_cache_dir()`가 이를 재사용하도록 정리
- 그 외 사업 판정기준, 지도 UI, road 원본 데이터, R6에서 이미 수정된 항목(단일노선 교차확인, road-facts 라우트)은 변경하지 않음
