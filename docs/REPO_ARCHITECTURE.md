# 저장소 구성·모듈 경계 및 운영 보호 기준

기준: 2026-11-10 / 정리 브랜치: `refactor/repository-structure-20261110`

## 현재 구성 (1차 정리)

- `app.py`: 서울도시정비 운영 FastAPI 진입점. 운영 룰·UI 동작을 변경하지 않는다.
- `market_renewal.py`, `seoul_facilities.py`, `ui_editor_backend.py`: 서울 관련 모듈. 본 정리에서 위치·동작 변경 금지.
- `dev_app.py`: 개발 진입점. `/dev-assets/style.css`, `/dev-assets/nonurban.js`만 정적으로 제공하며 다른 서버 소스는 노출하지 않는다.
- `development/review.py`: 전국 플랫폼의 기존 라우터와 세션. 서비스 객체 주입의 기존 결합은 아직 제거되지 않았다.
- `development/sources.py`: 전국의 데이터 연결·현황 수집. 구역계·자료 상태 포함.
- `development/report.py`, `development/cad.py`, `development/survey_sheet.py`: 보고서·도면 결과 생성.
- `development/land/`: 토지 현황 관련 기능.
- `development/building/`: 건축물 현황 관련 기능.
- `development/nature/`: 자연환경 현황 관련 기능.
- `tests/`: 현행 회귀검증.
- `tests/legacy_regression/`: 과거 릴리스별 검사. 보존하며 자동 운영 검증과 분리한다.

## 변경하지 않은 것

- 서울 정비사업 RULE·팝업·추천 로직과 기존 서울 자료 경로.
- 공통 FACT 스키마와 법정 판정 산식.
- 운영 `main` 및 Render 배포.
- 운영에 쓰이는지 아직 확인되지 않은 SHP/ZIP/SQLite 데이터와 과거 롤백 자산. 무단 삭제 금지.

## 향후 확정해야 할 아키텍처

허용: `facts`(수집·검증·현황) → `nationwide`(전국 사업별 RULE) / `seoul`(서울 사업별 RULE).

금지: 전국 사업 적격성 판단 → 서울 정비사업 RULE 호출, 서울 전용 판단 → 전국 개발 RULE 호출, 공통 FACT → 어느 사업의 적격성/추천 결과 덮어쓰기.

현재는 **완전한 코드 독립 구조가 아님**. `app.py`가 `development.review`를 등록하고,
`development/review.py`가 주입받은 서울 서비스 객체의 `_vworld_*` 함수를 호출한다.
이 연결은 운영 호환성 때문에 1차 정리에서 유지했으며 별도 통합 분석/FACT 공급 인터페이스 설계 후 제거한다.
AST import 검사는 직접 import만 감시하며 이 런타임 객체 결합을 완전히 차단하지 못한다.

## 1차 파일 이동 호환성 보정

- `/api/development/ui.js` → 저장소 최상위 `development-ui.js`를 계속 읽는다.
- `NATIONAL_DATA_DIR` 미설정 시 기본 위치 → 저장소 최상위 `national_data/`로 유지한다.
- 개발 시작 페이지 `index.html`, `nonurban.html`, `style.css`, `nonurban.js`는 아직 루트에 유지한다.
- 기존 `/dev-assets/` 서비스는 필요한 두 파일만 제공하고 Python 소스는 제공하지 않는다.
- Dockerfile의 `uvicorn app:app` 진입점과 서울 경로는 유지한다.

## 병합 전 필수 검증

1. 정적 Python/JS 문법과 모든 import 확인
2. 서울 FastAPI 시작·기본 API·자산 URL 스모크 검사
3. 전국 개발 화면·조회·현황 FACT·도면·통계·엑셀 검사
4. 서울 기존 RULE별 대상지 회귀검증 (운영장애 별도 조사)
5. 국가 개발/서울 시스템 간 잘못된 의존·결과 공유 검사
6. 실제 대상지와 API 및 실제 파일 다운로드 개별 검증

**이 기준을 모두 충족하기 전에는 `main` 병합 및 운영 배포 금지.**
