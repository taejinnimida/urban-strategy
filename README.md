# 도시검토 플랫폼 Web MVP v0.3.2

GitHub 웹 업로드를 쉽게 하기 위해 폴더 구조를 제거한 단일파일 배포판입니다.

## GitHub에 올릴 파일
압축을 풀면 아래 6개 파일만 있습니다.

- `app.py`
- `requirements.txt`
- `Dockerfile`
- `render.yaml`
- `.gitignore`
- `README.md`

이 6개를 모두 GitHub 저장소의 최상위에 업로드하세요.

## 현재 기능
- 웹 지도에서 Polygon / Rectangle 작성
- 대상구역 면적·ha·둘레 자동 계산
- 서울 주택정비형 재개발 Rule Engine
- 면적, 노후도, 과소필지, 6m 접도율, 호수밀도, 노후연면적, 주민동의 입력
- PASS / FAIL / REVIEW 및 근거 표시

## Render
저장소를 Render Web Service에 연결하고 Docker 방식으로 배포합니다.
`render.yaml`과 `Dockerfile`이 저장소 최상위에 있으면 됩니다.

## 중요
현재 v0.3.2는 공간데이터 자동수집 전 단계입니다.
구역면적은 지도에서 자동 계산하고 나머지 정비지표는 직접 입력합니다.
