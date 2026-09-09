# v2.5.0 R5 검증기록 — 역세권활성화 350m·배제조건·특성관리지구

기준본: `urban-strategy-v2.5.0-20260909-table-streetblock-order-r4.zip`

## 변경 범위

### 1. 역세권 250m / 350m 자격 판정
- 250m 이내: 기본 PASS
- 250~350m:
  - 공식 환승확정(2개 노선 이상) → 350m PASS
  - 역과 대상지가 동일한 도심·광역중심·지역중심 범역 → 350m PASS
  - 공식 비환승 확정 + 중심지 350m 경로 비해당 → 250m 기준 FAIL
  - 환승정보 미확정 → REVIEW
- 350m 초과: FAIL
- 가로구역 포함률은 역세권 직접거리 판정을 끌어내리지 않고 독립항목으로 유지

백엔드가 `CONFIRMED_SINGLE_LINE`으로 반환한 비환승 Fact를 프론트가 `null`로 버리던 문제도 수정하였다.

### 2. 역세권활성화 배제조건
- 정비구역·정비예정구역: 기존 공식 SHP Fact 유지
- 재정비촉진지구: 공식 중첩 확인 시 원칙 FAIL, 존치관리구역·정비구역 해제 예외는 후속 재확인
- 서울도심 특성관리지구: 공식도면 기반 복원 GeoJSON 연결
- 소규모주택정비: 공식 정비구역 현황자료에서 소규모주택정비 사업구역이 확인되면 FAIL
- 도시계획시설: 사용자 요구에 따라 자동 진입 하드게이트에서 제외하고 INFO/후속협의로 표시

### 3. 특성관리지구 검토도면
- 사이트 분석에 독립 카드 및 미니맵 추가
- 공식도면 주황색 면을 벡터화한 복원자료 표시
- 60m 경계 안전대 적용
- `DERIVED_FROM_OFFICIAL_PLAN_MAP`으로 source_type 명시

## 검증 결과

### JavaScript 문법
- `node --check` PASS

### 기존 R4 전용 회귀검사
- `python targeted_regression_checks.py`
- **ALL TARGETED CHECKS PASSED**

### R5 추가 회귀검사
- `python targeted_regression_checks_r5.py`
- **ALL R5 TARGETED CHECKS PASSED**

동적 검증 사례:
- 비환승 확정 + 지구중심 + 305.1m → 역세권 direct **FAIL**, overall **FAIL**
- 환승 확정 + 305.1m → **PASS**
- 환승여부 미확정 + 305.1m → **REVIEW**
- 4.6m + 가로구역 미확보 → 역세권 **PASS**, 가로구역 **REVIEW**
- 400m + 가로구역 60% → 역세권 **FAIL**, 가로구역 **PASS**
- 특성관리지구 60m 내부 안전영역 → **FAIL**
- 특성관리지구 경계 오차대 → **REVIEW**
- 특성관리지구 충분한 외부 → **PASS**

### 원본 regression_checks.py
원본 검사기는 수정하지 않았다. R4 배포 ZIP 자체에 `uq181_legal.zip`이 포함되지 않아 동일하게 두 번째 `renewal spatial` 검사에서 `FileNotFoundError`로 중단된다.

실행결과:
- `measurement` PASS
- 이후 `/uq181_legal.zip` 누락으로 중단

이는 R5 코드 실패가 아니라 R4 기준 배포패키지의 기존 회귀검사 의존파일 누락 상태이다. 검사기를 변경하여 억지로 PASS시키지 않았다.

## 기준본 보존 확인

R4 대비 다음 파일은 SHA-256 동일:
- `app.py`
- `regression_checks.py`
- `road_shp_seoul.zip`
- `targeted_regression_checks.py`

수정 핵심은 `app.html`과 R5 검증·복원자료 추가에 한정하였다.
