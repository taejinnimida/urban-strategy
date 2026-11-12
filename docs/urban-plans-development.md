# Urban Planning Platform — 관련계획 개발 기준

기준일: 2026-11-12

| 구분 | 적용 명칭·상태 |
|---|---|
| 프로젝트 표시명 | Urban Planning Platform (도시계획 플랫폼) |
| GitHub 저장소 | `taejinnimida/urban-strategy` 유지 |
| 기초현황 개발 브랜치 | `urban-analysis` |
| 상위 및 관련계획 | `urban-plans` — 위 브랜치의 독립 기능 모듈 |
| 경관성 검토 브랜치 | `urban-landscape-review` |
| 보관 기준 | `main` |

## 전환 전후

- 초기 관련계획 작업은 `feature/related-plans-20261111`, 커밋 `8f051ac37ebf59caa72f8a66a867188b7ba88b3c`에 있다.
- 새 지침과 개발 기준 전환에 따라 이후 관련계획 모듈 파일은 기존 `urban-analysis` 브랜치에서 관리한다. 별도의 `urban-plans` 브랜치는 만들지 않는다.
- 이전 날짜형 브랜치는 기존 작업 기록으로 유지한다. 전체 브랜치를 병합하거나 삭제하지 않는다.
- 저장소 이름 변경 계획과 실제 변경을 구분한다. 최신 사용자 지시에 따라 저장소 `urban-strategy`와 `main`을 유지한다.

## 파일 범위

관련계획의 독립 구현 파일(`related_plans.py`, `plan_catalog_assets/`), 자체 검사(`tests/test_related_plans.py`), 경계 가공 스크립트, 관련 문서를 반영한다. 기존 구현 파일명과 `/plans`, `/api/related-plans` 경로를 유지한다.

기존 작업 브랜치에 있던 `app.py` 라우터 등록과 `development-ui.js` 진입 버튼 변경은 이번 개발 브랜치 반영에 포함하지 않는다. 공통 진입점 연결은 별도 통합 단계에서 협의한다. `app.html`, `dev_app.py`, Dockerfile, 공통 의존성·기존 CI도 변경하지 않는다.

## 동시 작업 보호

반영 직전에 `urban-analysis`의 최신 커밋과 파일 충돌 여부를 다시 확인한다. 최신 트리를 기반으로 모듈 파일만 추가하고 예상 커밋 비교를 포함해 브랜치를 갱신한다. 다른 창의 기초현황·건축물·도면 변경을 덮어쓰지 않는다.

명칭 기준 전환, 모듈 구현, GitHub 커밋, 공통 화면 연결, 운영 배포를 별도 단계로 기록한다. 이번 작업에서 Render 소스·웹훅·서비스 경로를 변경하거나 운영 배포를 수행하지 않는다.
