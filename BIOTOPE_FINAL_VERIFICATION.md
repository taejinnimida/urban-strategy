# v2.5.0 r22 — 상생주택 비오톱1등급 최종 보완

기준본: `app_v2.5.0_r22_factory-road-safe-popup-final.html` + `app_v2.5.0_r22_road-final.py`

## 반영 범위
- 상생주택 비오톱1등급을 `biotope_seoul.zip` 내장 SHP에서 직접 공간교차.
- 대상지 중첩면적(㎡), 중첩률(%), 실제 클립도형을 반환/표시.
- 유형평가 또는 개별평가 중 하나라도 `1등급`인 도형만 사용.
- PNU가 없어도 비오톱 분석 가능.
- 공익용산지 NED 조회 실패/미실행이 이미 성공한 비오톱 결과를 지우지 않음.
- 비오톱 SHP 실패 시에만 기존 NED 필지단위 결과를 fallback으로 사용.
- 상생주택 공간현황 Fact와 기초검토서가 동일 비오톱 Fact를 참조.

## 보존 확인
- 도로 SERVER→VWorld fallback 진단 최종본 유지.
- 공장용도 공통 Fact 및 수기 fallback/안전매칭 최종본 유지.
- 안심주택 `openSafeHousingDetailSafely()` 팝업 수정 유지.
- 앱 버전은 `v2.5.0` 유지.

## 직접 검증
- HTML inline JS `node --check`: PASS
- `app.py`, `regression_checks.py` py_compile: PASS
- 비오톱 원본 로드: 12,816건
- 12,816건 모두 `유형평가=1등급` 또는 `개별평가=1등급`: PASS
- 실제 대상지 교차: matched / 면적·비율 반환 PASS
- 서울 외부 미교차: none / 0㎡ PASS
- FastAPI `/api/spatial/biotope-data-status`: 200 / BIOTOPE_GRADE1_READY
- FastAPI `/api/spatial/biotope-intersections`: 200 / matched
- PNU 없음: 비오톱 exact Fact 유지 PASS
- NED 오류: 비오톱 exact Fact 유지 PASS
- 기존 targeted regression: safe popup / factory common fact / road dataset / biotope exact fact PASS

## CUL220 처리
`CUL220_보전산지지역`은 속성상 `보전준보전산지`만 제공되어 공익용산지를 단독 구분할 수 없으므로 이번 패치에서는 공익용산지 자동확정 자료로 사용하지 않음.
