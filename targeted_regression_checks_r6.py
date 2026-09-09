from pathlib import Path
from unittest.mock import patch
from shapely.geometry import box, mapping
import subprocess, tempfile
import app

ROOT=Path(__file__).resolve().parent
HTML=(ROOT/'app.html').read_text(encoding='utf-8')

def ok(name, cond, detail=''):
    if not cond:
        raise AssertionError(f'{name}: {detail or "FAILED"}')
    print('PASS', name)

class FakeResp:
    def __init__(self, payload, status=200):
        self._payload=payload; self.status_code=status
    def raise_for_status(self):
        if self.status_code>=400: raise RuntimeError(f'HTTP {self.status_code}')
    def json(self): return self._payload

def direct_payload(name='한성대입구', line='4호선'):
    return {'SearchInfoBySubwayNameService':{'row':[{'STATION_NM':name,'LINE_NUM':line}]}}

def check_station_backend_cross_confirmation():
    app._STATION_DIRECT_PROBE_CACHE.clear()
    global_ref={'stations':[{'name':'한성대입구역','lines':['4호선'],'line_count':1,'transfer':None,'transfer_status':'UNRESOLVED','sources':['SearchSTNBySubwayLineInfo']}], 'status':'partial'}
    with patch.object(app,'_seoul_open_data_key_info',return_value=('dummy','SEOUL_OPEN_DATA_KEY')), \
         patch.object(app,'_seoul_station_line_reference',return_value=global_ref), \
         patch.object(app.requests,'get',return_value=FakeResp(direct_payload())):
        r=app._direct_station_line_probe('한성대입구',force=True)
    ok('Hansung two official paths => confirmed single line', r.get('transfer') is False and r.get('transfer_status')=='CONFIRMED_SINGLE_LINE', str(r))
    ok('Hansung confirmed line is 4', r.get('lines')==['4호선'], str(r.get('lines')))
    ok('cross-confirmation basis recorded', '교차확인' in str(r.get('confirmation_basis')), str(r.get('confirmation_basis')))

    app._STATION_DIRECT_PROBE_CACHE.clear()
    with patch.object(app,'_seoul_open_data_key_info',return_value=('dummy','SEOUL_OPEN_DATA_KEY')), \
         patch.object(app,'_seoul_station_line_reference',return_value=global_ref), \
         patch.object(app.requests,'get',side_effect=RuntimeError('mock reject')):
        r2=app._direct_station_line_probe('한성대입구',force=True)
    ok('single source only remains unresolved', r2.get('transfer') is None and r2.get('transfer_status')=='UNRESOLVED', str(r2))


def check_frontend_hansung_decision():
    a=HTML.index('function activationThresholdInfoForStation(')
    b=HTML.index('\nfunction activationThresholdForStation(',a)
    th=HTML[a:b]
    a=HTML.index('function activationStationCriterion(')
    b=HTML.index('\nfunction checkActivationFromFacts(',a)
    cr=HTML[a:b]
    js=th+'\n'+cr+r'''
function ae(a,b,m){if(a!==b)throw new Error(`${m}: ${a} != ${b}`)}\nconst district={value:'district',label:'동선지구중심(동북권)'};\nconst st={name:'한성대입구역',distance_m:305.0,transfer:false,line_data_complete:true,center_value:'district',center_label:'동선지구중심(동북권)'};\nconst t=activationThresholdInfoForStation(st,district);\nae(t.threshold_m,250,'threshold'); ae(t.status,'CONFIRMED','threshold status');\nconst d=activationStationCriterion({...st,threshold_m:t.threshold_m,threshold_status:t.status,threshold_reason:t.reason,block_share_pct:null},{});\nae(d.direct.status,'FAIL','direct'); ae(d.overall.status,'FAIL','station overall'); ae(d.block.status,'REVIEW','block independent');\nconst arterial='FAIL';\nconst location=(d.overall.status==='PASS'||arterial==='PASS')?'PASS':(d.overall.status==='FAIL'&&arterial==='FAIL')?'FAIL':'REVIEW';\nae(location,'FAIL','location type');\nconsole.log('PASS Hansung 305m => station FAIL + location FAIL');
'''
    js=js.replace('\\n','\n')
    with tempfile.NamedTemporaryFile('w',suffix='.js',delete=False,encoding='utf-8') as f:
        f.write(js); fn=f.name
    subprocess.run(['node',fn],check=True)


def check_road_and_streetblock_pipeline():
    paths=[getattr(r,'path',None) for r in app.app.routes]
    ok('road-facts route registered','/api/spatial/road-facts' in paths)
    geom=mapping(box(127.0045,37.5865,127.0075,37.5890))
    r=app.road_facts(app.RoadFactInput(geometry=geom,radius_m=750))
    ok('bundled road facts resolve',r.get('status') in {'resolved','partial'} and len(r.get('manage_features') or [])>0, f"status={r.get('status')} manage={len(r.get('manage_features') or [])}")
    layers=app._basic_unit_spatial_layers()
    ok('root cause exposed: basic-unit missing',layers.get('available') is False and '기초단위구' in str(layers.get('reason')),str(layers))
    sb=app.analyze_street_block(geom,[],r.get('manage_features') or [],500,r.get('surface_features') or [],r.get('rw_features') or [],4,True)
    ok('missing basic unit never becomes false FAIL',sb.get('status')=='unavailable' and (sb.get('metadata') or {}).get('basic_unit_available') is False,str(sb.get('metadata')))
    ok('frontend shows basic-unit missing REVIEW','확인필요 · SGIS 기초단위구 자료 미설치' in HTML and '확인필요 · 기초단위구 자료' in HTML)


def check_frontend_transfer_guard():
    ok('confirmed single line accepted',"official?.transfer_status==='CONFIRMED_SINGLE_LINE'" in HTML and 'const lineDataComplete=transferKnown;' in HTML)
    ok('contradictory multiline guard','const contradictoryMultiLine=' in HTML and '!contradictoryMultiLine' in HTML)

if __name__=='__main__':
    check_station_backend_cross_confirmation()
    check_frontend_transfer_guard()
    check_frontend_hansung_decision()
    check_road_and_streetblock_pipeline()
    print('ALL R6 TARGETED CHECKS PASSED')
