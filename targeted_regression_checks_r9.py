from pathlib import Path
import importlib.util
import re
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent
HTML = (ROOT / 'app.html').read_text(encoding='utf-8')
PYTXT = (ROOT / 'app.py').read_text(encoding='utf-8')


def ok(cond, name, detail=''):
    if not cond:
        raise AssertionError(f'{name}: {detail or "FAILED"}')
    print('PASS', name)

# 1) The exact production failure must be removed: no browser fetch(mode:cors).
ok("mode:'cors'" not in HTML and 'mode:"cors"' not in HTML, 'no browser CORS fetch remains')

# 2) Ledger path is server-proxy first, and browser VWorld is fallback only.
ledger_m = re.search(r'async function fetchLandLedgerBrowser\(pnu\)\{(.*?)\n\}\n\nfunction parcelGeometryArea', HTML, re.S)
ok(bool(ledger_m), 'fetchLandLedgerBrowser found')
ledger = ledger_m.group(1)
proxy_pos = ledger.find("fetch('/api/land/ledger-one'")
jsonp_pos = ledger.find('vworldJsonp(')
ok(proxy_pos >= 0, 'ledger server proxy present')
ok(jsonp_pos < 0 or proxy_pos < jsonp_pos, 'ledger proxy precedes JSONP fallback')
ok('if(r.ok && d.vworld_ready===true)return null;' in ledger, 'ledger avoids duplicate official retries after valid server lookup')

# 3) Characteristics/official-area paths also use server proxy first.
char_m = re.search(r'async function fetchLandCharacteristicsBrowser\(pnu\)\{(.*?)\n\}\nasync function fetchLandLedgerBrowser', HTML, re.S)
ok(bool(char_m), 'fetchLandCharacteristicsBrowser found')
char = char_m.group(1)
ok("fetch('/api/land/characteristics-one'" in char, 'characteristics server proxy present')
ok(char.find("fetch('/api/land/characteristics-one'") < char.find('vworldJsonp('), 'characteristics proxy precedes JSONP fallback')
area_m = re.search(r'async function fetchOfficialAreaBrowser\(pnu\)\{(.*?)\n\}\n\nasync function mapLimit', HTML, re.S)
ok(bool(area_m), 'fetchOfficialAreaBrowser found')
ok("fetch('/api/land/characteristics-one'" in area_m.group(1), 'official area uses server proxy')

# 4) Backend owns all NED CORS-sensitive calls and has characteristics fallback.
ok('@app.post("/api/land/ledger-one")' in PYTXT, 'ledger proxy route registered')
ok('@app.post("/api/land/characteristics-one")' in PYTXT, 'characteristics proxy route registered')
ok('def _server_land_characteristics_vworld' in PYTXT, 'server characteristics helper present')
route_m = re.search(r'@app\.post\("/api/land/ledger-one"\)(.*?)@app\.post\("/api/land/characteristics-one"\)', PYTXT, re.S)
ok(bool(route_m), 'ledger route block found')
route = route_m.group(1)
pos1 = route.find('_server_land_ledger_vworld')
pos2 = route.find('_server_land_ledger_legacy_data_go')
pos3 = route.find('_server_land_characteristics_vworld')
ok(0 <= pos1 < pos2 < pos3, 'server fallback order ledger -> legacy -> characteristics')
ok('"vworld_ready": bool(_vworld_key())' in route, 'server reports VWorld readiness for client retry guard')

# 5) Dynamic endpoint behavior without any internet request.
spec = importlib.util.spec_from_file_location('urban_r9_app', ROOT / 'app.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
client = TestClient(mod.app)

orig_ledger = mod._server_land_ledger_vworld
orig_legacy = mod._server_land_ledger_legacy_data_go
orig_char = mod._server_land_characteristics_vworld
orig_key = mod._vworld_key
try:
    calls = []
    mod._vworld_key = lambda: 'test-key'
    mod._server_land_ledger_vworld = lambda p: calls.append(('ledger', p)) or {'pnu': p, 'lndpclAr': 123.4, '_route': 'test_ledger'}
    mod._server_land_ledger_legacy_data_go = lambda p: calls.append(('legacy', p)) or None
    mod._server_land_characteristics_vworld = lambda p: calls.append(('char', p)) or None
    r = client.post('/api/land/ledger-one', json={'pnu': '1111010100100010000'})
    ok(r.status_code == 200, 'ledger proxy dynamic 200')
    d = r.json()
    ok(d['record']['lndpclAr'] == 123.4 and calls == [('ledger', '1111010100100010000')], 'ledger success short-circuits fallbacks')

    calls.clear()
    mod._server_land_ledger_vworld = lambda p: calls.append(('ledger', p)) or None
    mod._server_land_ledger_legacy_data_go = lambda p: calls.append(('legacy', p)) or None
    mod._server_land_characteristics_vworld = lambda p: calls.append(('char', p)) or {'pnu': p, 'lndpclAr': 88.8, '_route': 'test_char'}
    r = client.post('/api/land/ledger-one', json={'pnu': '1111010100100010000'})
    ok(r.status_code == 200, 'characteristics fallback dynamic 200')
    d = r.json()
    ok(d['record']['lndpclAr'] == 88.8, 'characteristics fallback record returned')
    ok(d['source']['operation'] == 'getLandCharacteristics', 'fallback source operation exposed')
    ok(calls == [('ledger', '1111010100100010000'), ('legacy', '1111010100100010000'), ('char', '1111010100100010000')], 'dynamic fallback order exact')

    calls.clear()
    r = client.post('/api/land/characteristics-one', json={'pnu': '1111010100100010000'})
    ok(r.status_code == 200 and r.json()['record']['lndpclAr'] == 88.8, 'characteristics proxy dynamic 200')
finally:
    mod._server_land_ledger_vworld = orig_ledger
    mod._server_land_ledger_legacy_data_go = orig_legacy
    mod._server_land_characteristics_vworld = orig_char
    mod._vworld_key = orig_key

print('ALL R9 TARGETED CHECKS PASSED')
