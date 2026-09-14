from pathlib import Path
import importlib.util, json, math, re, subprocess, sys

BASE=Path(__file__).resolve().parent
HTML=(BASE/'app.html').read_text(encoding='utf-8')
PY=(BASE/'app.py').read_text(encoding='utf-8')
META=json.loads((BASE/'hill_terrain_20m_meta.json').read_text(encoding='utf-8'))
checks=[]
def ck(name,cond):
    assert cond,name; checks.append(name); print('PASS',name)

ck('R37 hill card visible', 'id="siteDetail_hill"' in HTML and 'site-analysis-hidden" id="siteDetail_hill"' not in HTML)
ck('R37 hill card follows natural environment', HTML.index('id="siteDetail_sharedConservation"') < HTML.index('id="siteDetail_hill"') < HTML.index('id="siteDetail_building"'))
for marker in ['spHillElevation','spHillElevationRange','spHillSlopeMean','spHillSlopeMax','spHillElev40Area','spHillSlope10Area','spHillCombinedArea','spHillDistance']:
    ck('R37 UI '+marker, f'id="{marker}"' in HTML)
ck('R37 terrain source disclaimer', '서울시 공식 구릉지 SHP가 아니라' in HTML)
ck('R37 auto pipeline calls hill', "safeAnalysisStep('구릉지 지형 FACT',analyzeHillZones" in HTML)
ck('R37 hill fact store normalizes metrics', 'elevation_m:Number.isFinite(Number(hillAnalysis.elevation?.representative_m))' in HTML and 'reference_areas:{...(hillAnalysis.reference_areas||{})}' in HTML)
ck('R37 station complex uses hill FACT not missing SHP string', "플랫폼 산출 지형 FACT 확보" in HTML and "구릉지','구릉지 제외·연접 여부','공개 SHP 미확보'" not in HTML)
ck('R37 longterm keeps hill legal decision review', '구릉지 연접 여부와 법적 적용은 운영기준·현장조건 확인 후 반영' in HTML)
ck('R37 backend source type', 'HILL_SOURCE_TYPE = "PLATFORM_DERIVED_REFERENCE"' in PY)
ck('R37 backend no official SHP loader', 'HILL_SOURCE_CANDIDATES' not in PY and 'def _hill_spatial_index' not in PY)
ck('R37 backend uses 20m raster facts', 'HILL_GRID_META_FILE = "hill_terrain_20m_meta.json"' in PY and 'def _hill_grid_arrays' in PY)
ck('R37 backend exact partial-cell area intersection', 'inter = cell.intersection(site_metric)' in PY)
ck('R37 backend separates reference thresholds', 'HILL_REFERENCE_ELEVATION_M = 40.0' in PY and 'HILL_REFERENCE_SLOPE_DEG = 10.0' in PY)
ck('R37 health readiness', '"hill_terrain_model"' in PY)
ck('R37 meta EPSG5174', META['source_crs']=='EPSG:5174' and META['grid_crs']=='EPSG:5174')
ck('R37 meta official source date', META['source_extract_date']=='2025-03-18')
ck('R37 meta resolution', float(META['resolution_m'])==20.0)
ck('R37 holdout validation recorded', META['validation']['2025_spot_height_holdout_10pct']['n']==4500 and META['validation']['2025_spot_height_holdout_10pct']['mean_abs_error_m']<2.0)
for fn in ['hill_elevation_20m_i16.zlib','hill_slope_20m_i16.zlib']:
    ck('R37 data file '+fn,(BASE/fn).is_file() and (BASE/fn).stat().st_size>100000)

# Runtime factual sanity: flat land must not be reference hill; clear hill must intersect.
spec=importlib.util.spec_from_file_location('app_r37',BASE/'app.py'); mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
def square(lon,lat,d=0.001):
    return {'type':'Polygon','coordinates':[[[lon-d,lat-d],[lon+d,lat-d],[lon+d,lat+d],[lon-d,lat+d],[lon-d,lat-d]]]}
flat=mod.analyze_hill_intersections(square(126.9240,37.5210)) # Yeouido sanity point
hill=mod.analyze_hill_intersections(square(126.9882,37.5512)) # Namsan sanity point
ck('R37 runtime flat sanity', flat['status']=='confirmed' and flat['intersects'] is False and flat['elevation']['representative_m']<40 and flat['overlap_pct']==0.0)
ck('R37 runtime hill sanity', hill['status']=='confirmed' and hill['intersects'] is True and hill['elevation']['representative_m']>40 and hill['overlap_pct']>50)
ck('R37 runtime map layers', len(hill['context_features'])>=3 and len(hill['overlaps'])>=1)
ck('R37 runtime source is model reference', hill['metadata']['source_type']=='PLATFORM_DERIVED_REFERENCE')

# Syntax checks.
subprocess.run([sys.executable,'-m','py_compile',str(BASE/'app.py')],check=True)
ck('R37 backend python syntax',True)
scripts=re.findall(r'<script[^>]*>(.*?)</script>',HTML,re.S)
inline=BASE/'_r37_inline.js';inline.write_text('\n'.join(scripts),encoding='utf-8')
subprocess.run(['node','--check',str(inline)],check=True,stdout=subprocess.DEVNULL)
ck('R37 inline javascript syntax',True)
print(f'ALL R37 TARGETED CHECKS PASS ({len(checks)}/{len(checks)})')
