from pathlib import Path
import json, re, subprocess, tempfile
from shapely.geometry import shape

base = Path(__file__).resolve().parent
html = (base / 'app.html').read_text(encoding='utf-8')
py = (base / 'app.py').read_text(encoding='utf-8')
ref = json.loads((base / 'route_commercial_reference.geojson').read_text(encoding='utf-8'))
checks = []

def check(name, cond):
    if not cond:
        raise AssertionError(name)
    print('PASS', name)
    checks.append(name)

# Data source integrity.
meta = ref.get('metadata') or {}
features = ref.get('features') or []
check('route reference featurecollection', ref.get('type') == 'FeatureCollection')
check('route reference source type is model', meta.get('source_type') == 'MODEL_REFERENCE')
check('route reference explicitly non-legal', meta.get('legal_source') is False)
check('route reference source crs fixed to 5174', meta.get('source_crs') == 'EPSG:5174')
check('route reference has dissolved features', len(features) == 829)
check('route reference raw polyline count retained', meta.get('raw_polyline_count') == 1672)
check('route reference only for activation arterial', '역세권활성화 간선가로형' in str(meta.get('model_use')))

xs, ys = [], []
for f in features:
    g = shape(f['geometry'])
    minx, miny, maxx, maxy = g.bounds
    xs.extend([minx, maxx]); ys.extend([miny, maxy])
check('route reference WGS84 longitude sane', min(xs) > 126.7 and max(xs) < 127.3)
check('route reference WGS84 latitude sane', min(ys) > 37.3 and max(ys) < 37.8)
check('route reference geometries valid', all(shape(f['geometry']).is_valid and not shape(f['geometry']).is_empty for f in features))

# Backend path and source typing.
check('backend route reference path', 'ROUTE_COMMERCIAL_REFERENCE_PATH = _data_path("route_commercial_reference.geojson")' in py)
check('backend route reference endpoint', '@app.get("/api/reference/route-commercial")' in py)
check('backend readiness includes model source', '"route_commercial_model": os.path.isfile(_data_path("route_commercial_reference.geojson"))' in py)
check('backend loader enforces non-legal metadata', '"source_type": "MODEL_REFERENCE"' in py and '"legal_source": False' in py)

# Frontend primary analysis uses only model reference for the line-commercial FACT.
start = html.index('async function analyzeActivationArterial()')
end = html.index('function updateActivationArterialBlockLink()', start)
analysis = html[start:end]
check('activation analysis loads model reference', 'loadRouteCommercialReference()' in analysis)
check('activation analysis no longer fetches UQ111', "fetchSpatialFeaturesBrowser('LT_C_UQ111'" not in analysis)
check('activation analysis no longer calls shape classifier', 'classifyLinearCommercialShape(' not in analysis)
check('activation analysis marks MODEL_REFERENCE', "source_type:'MODEL_REFERENCE'" in analysis)
check('road failure is context warning not source failure', '도로 문맥자료 조회 실패' in analysis)

# Route decision state.
d_start = html.index('function activationArterialRouteDecision')
d_end = html.index('async function analyzeActivationArterial()', d_start)
decision = html[d_start:d_end]
check('street block unknown stays review', "if(!blockKnown)return {status:'REVIEW'" in decision)
check('missing model reference stays review', "referenceFetchFailed||!referenceAvailable" in decision and "status:'REVIEW'" in decision)

with tempfile.NamedTemporaryFile('w', suffix='.js', encoding='utf-8', delete=False) as tf:
    tf.write(decision)
    tf.write(r'''
const cases = [
  ['missing', activationArterialRouteDecision({referenceFetchFailed:true,referenceAvailable:false,blockKnown:false}).status, 'REVIEW'],
  ['wait-block', activationArterialRouteDecision({referenceAvailable:true,blockKnown:false,candidateCount:1}).status, 'REVIEW'],
  ['site-pass', activationArterialRouteDecision({referenceAvailable:true,blockKnown:false,siteIncludes:true}).status, 'PASS'],
  ['block-pass', activationArterialRouteDecision({referenceAvailable:true,blockKnown:true,blockIncludes:true}).status, 'PASS'],
  ['confirmed-none', activationArterialRouteDecision({referenceAvailable:true,blockKnown:true,candidateCount:0}).status, 'FAIL']
];
for (const [n,v,e] of cases) {
  if (v !== e) { console.error(n,v,e); process.exit(1); }
}
''')
    decision_js = tf.name
r = subprocess.run(['node', decision_js], capture_output=True, text=True)
check('route decision state machine', r.returncode == 0)

# Re-link after scheme-specific street block is ready.
u_start = html.index('function updateActivationArterialBlockLink()')
u_end = html.index('function renderActivationArterialSpatialStatus()', u_start)
upd = html[u_start:u_end]
check('block relink uses full reference cache', 'routeCommercialReferenceCache?.features||[]' in upd)
check('block relink re-decides route status', 'activationArterialRouteDecision' in upd)

# UI/source disclosure.
check('ui labels model reference', '판정모형용 노선형 상업지역 참조도형' in html)
check('ui says not official legal geometry', '서울시 공식 법정도형이 아니라' in html)
check('fact source names model reference', "source:'MODEL_REFERENCE · 노선형 상업지역 판정용 참조도형" in html)
check('legacy 8-18 inference not primary', 'LEGACY DIAGNOSTIC ONLY (R35 이후 주 판정 미사용)' in html)

# R34 semiindustrial UI remains.
for marker in ('function semiindustrialCardIcon', 'semiindustrial-card-title', 'word-break:keep-all'):
    check('R34 semiindustrial UI retained: ' + marker, marker in html)

# Syntax checks.
blocks = re.findall(r'<script[^>]*>(.*?)</script>', html, re.S | re.I)
with tempfile.NamedTemporaryFile('w', suffix='.js', encoding='utf-8', delete=False) as tf:
    tf.write('\n'.join(blocks))
    js_path = tf.name
r = subprocess.run(['node', '--check', js_path], capture_output=True, text=True)
check('inline javascript syntax', r.returncode == 0)
r = subprocess.run(['python', '-m', 'py_compile', str(base / 'app.py')], capture_output=True, text=True)
check('backend python syntax', r.returncode == 0)

print(f'ALL R35 TARGETED CHECKS PASS ({len(checks)}/{len(checks)})')
