# 독립 도시현황 FACT 조사 서버

이 산출물은 서울도시정비 서버 `app.py`를 포함하지 않습니다. 기존 서울 현황분석과 별도로 운용하는 초기 도시현황 데이터 구축 애플리케이션입니다. 전국 도시개발사업의 법정 적격성 판단 기능도 포함하지 않습니다.

## 다른 환경에 배포

배포 산출물 ZIP을 압축 해제한 디렉터리에서 실행합니다.

```bash
python -m pip install -r requirements.txt
uvicorn dev_app:app --host 0.0.0.0 --port 8000
```

컨테이너 환경:

```bash
docker build -t urban-survey-facts .
docker run --rm -p 8000:8000 -e VWORLD_API_KEY -e VWORLD_DOMAIN=example.org urban-survey-facts
```

브라우저로 `http://localhost:8000/`에 접속합니다. `/docs`에서 독립 API를 확인할 수 있습니다.

## 현재 기능과 자료 한계

- 지리 구역계 수신, 독립 FACT 조회 API, 필지·건축물 현황표 생성기, XLSX/CAD 도면 생성 경로를 포함합니다.
- 공공데이터 연결은 API 키, 자료 종류, 제공기관 상태 및 실제 원자료 등록에 따라 달라집니다. 자동화된 실제 대상지 출력 검증 전까지 운영 서비스로 간주하지 마세요.
- 토지대장, 개별공시지가, 건축물대장의 일부 수집기는 아직 독립 구현 전이므로 명시적으로 `SOURCE_NOT_IMPLEMENTED`를 반환하고 서울 서버로 우회하지 않습니다.
- `national_data/`는 공식 자료가 포함될 수 있어 기본 ZIP에 자동 동봉하지 않습니다. 해당 자료는 적법한 출처·최신성 확인 후 `NATIONAL_DATA_DIR`로 별도 제공하세요.
- 어떤 기능도 서울 정비사업 RULE을 호출하지 않습니다. 현황 분석 수치도 법정 사업 판단을 의미하지 않습니다.

## 독립 패키지 구성

`dev_app.py`, `survey_sources.py`, `survey.html`, `survey-ui.js`, `facts/`, `development/`, `style.css`, `requirements.txt`, `Dockerfile`.

## 배포 전 검사

다른 폴더에 압축 해제해 `python -c "from dev_app import app; assert all('seoul' not in r.path for r in app.routes)"`로 기본 라우트를 확인할 수 있습니다. 실제 자료 조회는 API 키와 원자료 출처를 별도로 검증해야 합니다.

이 패키지는 GitHub `main`과 Render 서울도시정비 운영판에 영향을 주지 않습니다.
