import importlib.util, os, sys, time
from pathlib import Path

APP_PATH = Path(__file__).with_name('app.py')
spec = importlib.util.spec_from_file_location('urban_r15_app', APP_PATH)
app = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = app
spec.loader.exec_module(app)

PNU='1111010100100010000'

def reset_cache():
    with app.LAND_LEDGER_CACHE_LOCK:
        app.LAND_LEDGER_CACHE.clear()

def rec(route, date='2026-09-01', area=100.0):
    return {'pnu':PNU,'lndpclAr':area,'lastUpdtDt':date,'_route':route,'lndcgrCodeNm':'대'}

def assert_true(cond,msg):
    if not cond:
        raise AssertionError(msg)
    print('PASS',msg)

orig = {
    '_vworld_key': app._vworld_key,
    '_building_hub_key': app._building_hub_key,
    '_server_land_ledger_vworld': app._server_land_ledger_vworld,
    '_server_land_ledger_legacy_data_go': app._server_land_ledger_legacy_data_go,
    '_server_land_characteristics_vworld': app._server_land_characteristics_vworld,
}

try:
    app._vworld_key=lambda:'vw'
    app._building_hub_key=lambda:'dg'

    # 1) first successful *full ledger* wins without waiting for slower peer.
    reset_cache()
    def slow_vw(pnu, timeout=6):
        time.sleep(0.45); return rec('vw', area=101)
    def fast_dg(pnu, timeout=6):
        time.sleep(0.04); return rec('dg', area=102)
    def never_char(pnu, timeout=6):
        raise AssertionError('characteristics must not run after a full-ledger success')
    app._server_land_ledger_vworld=slow_vw
    app._server_land_ledger_legacy_data_go=fast_dg
    app._server_land_characteristics_vworld=never_char
    t=time.perf_counter(); out=app._resolve_land_ledger(PNU); elapsed=time.perf_counter()-t
    assert_true(out['selected_source']=='data_go_ladfrl','fast full-ledger route selected')
    assert_true(out['record']['lndpclAr']==102,'full-ledger payload preserved')
    assert_true(elapsed < 0.20,'resolver does not wait for slower full-ledger peer')
    # allow slow background task to finish before monkeypatching next scenario
    time.sleep(0.5)

    # 2) characteristics remains lower priority and only runs after both full ledgers fail.
    reset_cache()
    calls=[]
    def no_vw(pnu, timeout=6): calls.append('vw'); time.sleep(0.03); return None
    def no_dg(pnu, timeout=6): calls.append('dg'); time.sleep(0.05); return None
    def yes_char(pnu, timeout=6): calls.append('char'); time.sleep(0.01); return rec('char', area=103)
    app._server_land_ledger_vworld=no_vw
    app._server_land_ledger_legacy_data_go=no_dg
    app._server_land_characteristics_vworld=yes_char
    out=app._resolve_land_ledger(PNU)
    assert_true(out['selected_source']=='vworld_land_characteristics','characteristics used only as fallback')
    assert_true('vw' in calls and 'dg' in calls and calls[-1]=='char','both full-ledger routes tried before characteristics')

    # 3) positive cache prevents repeat source calls.
    reset_cache(); counts={'vw':0,'dg':0,'char':0}
    def cvw(pnu, timeout=6): counts['vw']+=1; return rec('vw', area=104)
    def cdg(pnu, timeout=6): counts['dg']+=1; time.sleep(0.1); return None
    def cchar(pnu, timeout=6): counts['char']+=1; return None
    app._server_land_ledger_vworld=cvw; app._server_land_ledger_legacy_data_go=cdg; app._server_land_characteristics_vworld=cchar
    first=app._resolve_land_ledger(PNU); second=app._resolve_land_ledger(PNU)
    assert_true(first['cache_hit'] is False and second['cache_hit'] is True,'positive PNU result cached')
    assert_true(counts['vw']==1 and counts['char']==0,'cached repeat avoids official-source calls')
    time.sleep(0.12)

    # 4) negative cache also prevents immediate repeat failures.
    reset_cache(); counts={'vw':0,'dg':0,'char':0}
    def nvw(pnu, timeout=6): counts['vw']+=1; return None
    def ndg(pnu, timeout=6): counts['dg']+=1; return None
    def nchar(pnu, timeout=6): counts['char']+=1; return None
    app._server_land_ledger_vworld=nvw; app._server_land_ledger_legacy_data_go=ndg; app._server_land_characteristics_vworld=nchar
    a=app._resolve_land_ledger(PNU); b=app._resolve_land_ledger(PNU)
    assert_true(a['record'] is None and b['record'] is None,'negative result remains unknown, not guessed')
    assert_true(b['cache_hit'] is True,'negative PNU result short-TTL cached')
    assert_true(counts=={'vw':1,'dg':1,'char':1},'negative cache avoids repeated failure chain')

    # 5) HTTP alias removed from normal retry list; single HTTPS service only.
    assert_true(app.LEGACY_LAND_LEDGER_URL.startswith('https://apis.data.go.kr/1611000/nsdi/eios/LadfrlService/'), 'legacy ledger uses one HTTPS endpoint')
    assert_true(not hasattr(app,'LEGACY_LAND_LEDGER_URLS'),'duplicate HTTP/HTTPS sequential URL list removed')

    # 6) endpoint schema remains compatible while exposing diagnostics.
    reset_cache()
    app._server_land_ledger_vworld=lambda pnu,timeout=6: rec('vw',area=105)
    app._server_land_ledger_legacy_data_go=lambda pnu,timeout=6: None
    app._server_land_characteristics_vworld=lambda pnu,timeout=6: None
    payload=app.land_ledger_one(app.LandLedgerOneInput(pnu=PNU))
    assert_true(payload['record']['lndpclAr']==105,'ledger-one still returns record schema')
    assert_true(payload['source']['operation']=='ladfrlList','ledger-one source operation preserved')
    assert_true('cache_hit' in payload and 'elapsed_ms' in payload,'ledger-one exposes performance diagnostics')

    print('ALL R15 TARGETED CHECKS PASS')
finally:
    for k,v in orig.items(): setattr(app,k,v)
