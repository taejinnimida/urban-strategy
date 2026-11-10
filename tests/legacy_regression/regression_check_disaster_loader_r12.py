from pathlib import Path
import tempfile, zipfile, importlib.util, sys
import shapefile
from pyproj import CRS

base=Path(__file__).resolve().parent
tmp=Path(tempfile.mkdtemp(prefix='disaster_unit_'))
stem=tmp/'test'
w=shapefile.Writer(str(stem), shapeType=shapefile.POLYGON, encoding='cp949')
w.field('NAME','C',50)
# polygon around 127,37.5
ring=[[126.99,37.49],[127.01,37.49],[127.01,37.51],[126.99,37.51],[126.99,37.49]]
w.poly([ring]); w.record('테스트')
w.close()
(stem.with_suffix('.prj')).write_text(CRS.from_epsg(4326).to_wkt(),encoding='utf-8')
zip_path=tmp/'test.zip'
with zipfile.ZipFile(zip_path,'w') as z:
    for ext in ['.shp','.shx','.dbf','.prj']:
        z.write(stem.with_suffix(ext),arcname='test'+ext)

spec=importlib.util.spec_from_file_location('urban_app_unit',base/'app.py')
mod=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=mod
spec.loader.exec_module(mod)
mod._download_official_zip=lambda url,cache_name:str(zip_path)
site={'type':'Polygon','coordinates':[[[126.995,37.495],[127.005,37.495],[127.005,37.505],[126.995,37.505],[126.995,37.495]]]}
r=mod._analyze_remote_polygon_zip(site,url='x',cache_name='x.zip',source_label='unit',default_epsg=4326)
assert r['known'] is True, r
assert r['present'] is True, r
assert r['overlap_area_m2'] > 0, r
assert r['overlap_pct'] > 0, r
print('PASS disaster remote SHP loader', round(r['overlap_area_m2'],1), round(r['overlap_pct'],1))
