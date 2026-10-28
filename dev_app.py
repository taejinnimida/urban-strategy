"""Isolated development entry point. Production remains app:app."""
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
import math

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pyproj import Geod
from shapely.geometry import shape

import app as seoul

BASE = Path(__file__).parent


@asynccontextmanager
async def lifespan(application):
    async with seoul.app.router.lifespan_context(seoul.app):
        yield


app = FastAPI(title="URBAN STRATEGY R104 DEV", lifespan=lifespan)
app.mount('/dev-assets', StaticFiles(directory=BASE / 'development'), name='development')


@app.get('/', response_class=HTMLResponse)
def modes():
    return (BASE / 'development/index.html').read_text(encoding='utf-8')


@app.get('/seoul', response_class=HTMLResponse)
def seoul_mode():
    response = seoul.home()
    content = response.body.decode('utf-8').replace('<body>', '<body><a href="/" style="position:fixed;bottom:12px;left:12px;z-index:2147483646;background:#12394c;color:white;padding:10px;border-radius:8px">DEV · 검토모드 선택</a>', 1)
    return HTMLResponse(content, headers={'Cache-Control': 'no-store'})


@app.get('/nonurban', response_class=HTMLResponse)
def nonurban_mode():
    return (BASE / 'development/nonurban.html').read_text(encoding='utf-8')


class Intake(BaseModel):
    jurisdiction: str = Field(default='', max_length=100)
    address: str = Field(default='', max_length=300)
    project: Literal['development', 'district', 'industrial', 'facility', 'building']
    area_m2: float | None = Field(default=None, gt=0, le=1e10, allow_inf_nan=False)
    zoning: str = Field(default='', max_length=100)
    notes: str = Field(default='', max_length=5000)
    geometry: dict | None = None


def boundary_fact(geometry):
    if geometry.get('type') not in ('Polygon', 'MultiPolygon'):
        raise ValueError('Polygon 또는 MultiPolygon 구역계가 필요합니다.')
    def points(value):
        if isinstance(value, list) and value and isinstance(value[0], (int, float)):
            yield value
        elif isinstance(value, list):
            for child in value:
                yield from points(child)
    coords = list(points(geometry.get('coordinates')))
    if not coords or len(coords) > 20000:
        raise ValueError('좌표는 1~20,000개까지 허용합니다.')
    if any(len(p) != 2 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in p) or not (124 <= p[0] <= 132 and 33 <= p[1] <= 39.5) for p in coords):
        raise ValueError('대한민국 범위의 WGS84 경도·위도 좌표가 필요합니다.')
    geom = shape(geometry)
    if geom.is_empty or not geom.is_valid:
        raise ValueError('구역계가 비어 있거나 자기교차 등 도형 오류가 있습니다.')
    from shapely.geometry.polygon import orient
    polygons = [geom] if geom.geom_type == 'Polygon' else geom.geoms
    area = sum(abs(Geod(ellps='WGS84').geometry_area_perimeter(orient(p, sign=1))[0]) for p in polygons)
    return {'area_m2': round(area, 2), 'source': '사용자 GeoJSON / WGS84 타원체 면적', 'geometry': geometry}


@app.post('/api/dev/nonurban/intake')
def intake(data: Intake):
    try:
        boundary = boundary_fact(data.geometry) if data.geometry else None
    except Exception as exc:
        raise HTTPException(422, '구역계 확인: ' + (str(exc) if isinstance(exc, ValueError) else '도형 형식을 확인하세요.'))
    subjects = [
        ('대상지·용도지역', '토지이용계획확인서, 관할 지자체 도시·군관리계획'),
        ('농지·산지', '농지·산지 해당 여부와 전용 관련 자료'),
        ('자연환경·보호구역', '공적 규제도면과 대상지 중첩자료'),
        ('도로·기반시설', '접도·도로 폭원 및 상하수도 공급 가능 자료'),
        ('제영향평가·행정절차', '사업유형·규모별 현행 법령과 예외조건'),
        ('관할 지자체 기준', '해당 시·군 조례·시행규칙·위임기준'),
    ]
    return {'version': 'R104-DEV', 'mode': 'nonurban', 'status': 'NOT_ASSESSED',
            'message': '입력자료 정리 결과입니다. 전국 공공자료 자동조회와 법정기준 판정은 아직 연결되지 않았습니다.',
            'inputs': data.model_dump(exclude={'geometry'}), 'boundary': boundary,
            'effective_area_m2': boundary['area_m2'] if boundary else data.area_m2,
            'area_source': boundary['source'] if boundary else '사용자 입력 / 미검증',
            'checks': [{'item': name, 'status': 'UNKNOWN', 'needed': source} for name, source in subjects]}


# All original Seoul APIs, authentication and static assets retain their original URLs.
app.mount('/', seoul.app)
