from pathlib import Path
import json
import struct
import subprocess
import sys
import zipfile

from pyproj import Transformer
from shapely.geometry import shape, mapping, box
from shapely.ops import transform as geom_transform
from starlette.requests import Request

ROOT = Path(__file__).resolve().parent
import app

passed=0
failed=0

def check(label, cond, detail=''):
    global passed,failed
    if cond:
        passed+=1; print('PASS',label,detail)
    else:
        failed+=1; print('FAIL',label,detail)

# Bundled sources + derived raster metadata.
check('landslide source archive bundled', (ROOT/'source_landslide_risk_2026_seoul_11.zip').is_file())
check('risk district source archive bundled', (ROOT/'source_natural_disaster_risk_district_seoul_202609.zip').is_file())
check('landslide RLE bundled', (ROOT/'landslide_risk_2026_seoul_10m_rle.zlib').is_file())
meta=json.loads((ROOT/'landslide_risk_2026_seoul_10m_meta.json').read_text(encoding='utf-8'))
check('landslide EPSG5181', meta.get('crs')=='EPSG:5181', meta.get('crs'))
check('landslide 10m grid', meta.get('pixel_width_m')==10.0 and meta.get('pixel_height_m')==10.0)
check('landslide nodata 127', meta.get('nodata')==127)
check('landslide grades 1-5', meta.get('grades')==[1,2,3,4,5])
check('landslide process date 2026-08-05', meta.get('source_process_date')=='2026-08-05')

# Local risk-district ZIP really carries 6 records and analyzer intersects exact geometry.
with zipfile.ZipFile(ROOT/'source_natural_disaster_risk_district_seoul_202609.zip') as zf:
    names=zf.namelist(); shp=next(n for n in names if n.lower().endswith('.shp')); shx=next(n for n in names if n.lower().endswith('.shx')); dbf=next(n for n in names if n.lower().endswith('.dbf'))
    import io, shapefile
    reader=shapefile.Reader(shp=io.BytesIO(zf.read(shp)), shx=io.BytesIO(zf.read(shx)), dbf=io.BytesIO(zf.read(dbf)), encoding='cp949', encodingErrors='replace')
    shapes=list(reader.iterShapes())
check('risk district source has 6 polygons', len(shapes)==6, len(shapes))
g=shape(shapes[0].__geo_interface__)
t=Transformer.from_crs(5186,4326,always_xy=True).transform
gw=geom_transform(t,g)
minx,miny,maxx,maxy=gw.bounds
site=box(minx-0.0001,miny-0.0001,maxx+0.0001,maxy+0.0001)
risk=app._analyze_local_polygon_zip(mapping(site), path=app.NATURAL_DISASTER_RISK_DISTRICT_ZIP, source_label='test', default_epsg=5186)
check('risk district exact overlap works', risk['known'] and risk['present'] and risk['overlap_area_m2']>0, round(risk.get('overlap_area_m2') or 0,1))
check('risk district source type bundled official shp', risk.get('source_type')=='BUNDLED_OFFICIAL_SHP')
check('risk district blank attrs not invented', 'NTFDATE' in risk.get('blank_attribute_counts',{}))

# Raster analyzer: first valid run must preserve grade and distinguish valid coverage from NoData.
meta2,raw=app._load_landslide_risk_reference()
r0,c0,c1,grade=struct.unpack_from('<IHHB',raw,0)
left,top,px,py=meta2['left'],meta2['top'],meta2['pixel_width_m'],meta2['pixel_height_m']
x=left+(c0+0.5)*px; y=top-(r0+0.5)*py
site_src=box(x-10,y-10,x+10,y+10)
t2=Transformer.from_crs(5181,4326,always_xy=True).transform
site_w=geom_transform(t2,site_src)
land=app._analyze_landslide_risk_raster(mapping(site_w))
check('landslide raster matched valid data', land['known'] and land['present'])
check('landslide grade preserved', any(d['grade']==grade and d['area_m2']>0 for d in land['distribution']), grade)
check('landslide returns nodata separately', land['nodata_value']==127 and land['nodata_area_m2'] is not None)
check('landslide source type bundled official raster', land.get('source_type')=='BUNDLED_OFFICIAL_RASTER')

# VWorld direct->proxy diagnostics retain failed direct route evidence.
class FakeResp:
    def __init__(self,status):
        self.status_code=status; self.headers={}; self.text='fake'
seq=[FakeResp(502),FakeResp(502)]
orig_get=app.requests.get
try:
    app.requests.get=lambda *a,**k: seq.pop(0)
    resp,route=app._vworld_get(app.VWORLD_DATA_URL, {'key':'x','domain':'https://example.com'}, timeout=1, proxy_timeout=1, referer_domain='https://example.com')
    diag=app._last_vworld_diagnostic()
finally:
    app.requests.get=orig_get
check('VWorld failed direct falls back to proxy', route=='vworld_proxy' and resp.status_code==502)
check('VWorld direct error retained', diag.get('direct_error')=='HTTP 502', diag)
check('VWorld proxy status retained', diag.get('proxy_status')==502, diag)
check('VWorld domain retained', diag.get('domain_sent')=='https://example.com', diag)

# Client origin is accepted only for the actual request host.
scope={'type':'http','method':'POST','path':'/api/spatial/planning-layers','headers':[(b'host',b'example.com')]}
req=Request(scope)
check('matching client origin accepted', app._validated_planning_client_origin('https://example.com',req)=='https://example.com')
check('foreign client origin rejected', app._validated_planning_client_origin('https://evil.example',req)==app._vworld_domain())

html=(ROOT/'app.html').read_text(encoding='utf-8')
check('frontend sends window origin', 'client_origin:window.location.origin' in html)
check('frontend exposes route diagnostics', 'direct_error:row.direct_error' in html and 'proxy HTTP' in html and 'firstErr.domain_sent' in html)
check('disaster UI includes risk district map', "'disaster_risk_map'" in html and '자연재해위험개선지구(원도형)' in html)
check('disaster UI includes landslide grade map', "'landslide_risk_map'" in html and '산사태위험지도 등급분포' in html)
check('disaster UI preserves NoData rule', '127은 위험 없음이 아니라 NoData/등급 미제공' in html)

rpy=subprocess.run([sys.executable,'-m','py_compile',str(ROOT/'app.py')],capture_output=True,text=True)
check('app.py compile',rpy.returncode==0,rpy.stderr.strip()[:200])
node=subprocess.run(['node','--check',str(ROOT/'inline_scripts.js')],capture_output=True,text=True)
check('browser JavaScript syntax',node.returncode==0,node.stderr.strip()[:200])

print(f'\nSUMMARY {passed}/{passed+failed} PASS')
if failed: raise SystemExit(1)
