from pathlib import Path
import json, re, subprocess, tempfile, math
from shapely.geometry import shape, Point
from shapely.ops import transform
from pyproj import Transformer

ROOT=Path(__file__).resolve().parent
APP=ROOT/'app.html'
BASE=APP.read_text(encoding='utf-8')
GJ=json.loads((ROOT/'downtown_characteristic_management_derived.geojson').read_text(encoding='utf-8'))

def check(name,cond,detail=''):
    if not cond: raise AssertionError(f'{name}: {detail or "FAILED"}')
    print('PASS',name)

def between(name,next_name):
    a=BASE.index('function '+name+'(')
    b=BASE.index('\nfunction '+next_name+'(',a)
    return BASE[a:b]

def station_dynamic():
    th=between('activationThresholdInfoForStation','activationThresholdForStation')
    cr=between('activationStationCriterion','checkActivationFromFacts')
    js=th+'\n'+cr+r'''
function ae(a,b,m){if(a!==b)throw new Error(`${m}: ${a} != ${b}`)}
const district={value:'district',label:'지구중심'};
let t=activationThresholdInfoForStation({transfer:false,line_data_complete:true,center_value:'district',center_label:'지구중심'},district);
ae(t.status,'CONFIRMED','single-line confirmed'); ae(t.threshold_m,250,'single-line threshold');
let s=activationStationCriterion({distance_m:305.1,threshold_m:t.threshold_m,threshold_status:t.status,threshold_reason:t.reason,block_share_pct:null},{});
ae(s.direct.status,'FAIL','Hanseong-like direct'); ae(s.overall.status,'FAIL','Hanseong-like overall'); ae(s.block.status,'REVIEW','independent block');
t=activationThresholdInfoForStation({transfer:true,line_data_complete:true,center_value:'district',center_label:'지구중심'},district);
s=activationStationCriterion({distance_m:305.1,threshold_m:t.threshold_m,threshold_status:t.status,threshold_reason:t.reason,block_share_pct:null},{});
ae(s.overall.status,'PASS','transfer 305.1');
t=activationThresholdInfoForStation({transfer:null,line_data_complete:false,center_value:'district',center_label:'지구중심'},district);
s=activationStationCriterion({distance_m:305.1,threshold_m:t.threshold_m,threshold_status:t.status,threshold_reason:t.reason,block_share_pct:null},{});
ae(s.overall.status,'REVIEW','unresolved transfer 305.1');
s=activationStationCriterion({distance_m:4.6,threshold_m:250,threshold_status:'CONFIRMED',block_share_pct:null},{});
ae(s.overall.status,'PASS','4.6 direct'); ae(s.block.status,'REVIEW','4.6 block pending');
s=activationStationCriterion({distance_m:400,threshold_m:250,threshold_status:'CONFIRMED',block_share_pct:60},{});
ae(s.overall.status,'FAIL','400 direct'); ae(s.block.status,'PASS','400 block independent');
console.log('PASS station dynamic cases');
'''
    with tempfile.NamedTemporaryFile('w',suffix='.js',delete=False,encoding='utf-8') as f:
        f.write(js); path=f.name
    subprocess.run(['node',path],check=True)

def vector_dynamic():
    meta=GJ.get('metadata') or {}
    check('derived source type',meta.get('source_type')=='DERIVED_FROM_OFFICIAL_PLAN_MAP')
    check('review margin 60m',float(meta.get('review_margin_m'))==60)
    check('georef error recorded',float(meta.get('georef_rmse_m',999))<60 and float(meta.get('georef_max_control_error_m',999))<60)
    check('polygon count',len(GJ.get('features') or [])>=20)
    to5179=Transformer.from_crs('EPSG:4326','EPSG:5179',always_xy=True).transform
    polys=[transform(to5179,shape(f['geometry'])) for f in GJ['features']]
    cores=[g.buffer(-60) for g in polys if not g.is_empty]
    cores=[g for g in cores if not g.is_empty and g.area>100]
    check('has 60m core polygons',len(cores)>0)
    core_union=None
    # classification equivalent to frontend (individual fragment buffers)
    def status(site):
        core=any(site.intersects(g) for g in cores)
        if core:return 'FAIL'
        raw=any(site.intersects(g) for g in polys)
        near=any(site.intersects(g.buffer(60)) for g in polys)
        return 'REVIEW' if raw or near else 'PASS'
    p=cores[0].representative_point(); check('deep interior => FAIL',status(p.buffer(3))=='FAIL')
    # seek an outer-only point that is not within any 60m inward core
    review_site=None
    for g in sorted(polys,key=lambda x:x.area,reverse=True):
        ring=g.buffer(50).difference(g.buffer(5))
        if ring.is_empty:continue
        rp=ring.representative_point().buffer(2)
        if status(rp)=='REVIEW': review_site=rp; break
    check('boundary uncertainty => REVIEW',review_site is not None)
    minx,miny,maxx,maxy=polys[0].bounds
    allmaxx=max(g.bounds[2] for g in polys); allmaxy=max(g.bounds[3] for g in polys)
    check('far outside => PASS',status(Point(allmaxx+1000,allmaxy+1000).buffer(5))=='PASS')
    print('PASS vector dynamic cases')

def main():
    check('HTML JS syntax marker','const DOWNTOWN_CHARACTERISTIC_MANAGEMENT_FC=' in BASE)
    check('site analysis card','id="siteDetail_downtownCharacteristic"' in BASE and 'id="ccDowntownCharacteristicMiniMap"' in BASE)
    check('FACT store link','spatial_evidence:{downtown_characteristic:downtownCharacteristicSpatialEvidenceFacts()' in BASE)
    check('boundary-safe decision','coreArea>=.2' in BASE and "status='FAIL'" in BASE and "else if(rawArea>=.2||near){status='REVIEW'" in BASE)
    check('confirmed false preserved','function normalizeOfficialTransfer(v){return v===true?true:v===false?false:null;}' in BASE)
    check('confirmed single accepted',"official?.transfer_status==='CONFIRMED_SINGLE_LINE'" in BASE and 'const lineDataComplete=transferKnown;' in BASE)
    check('activation characteristic hard gate',"schemeRow('서울도심 특성관리지구'" in BASE and "locator:'2-1-3 다목 1)'" in BASE)
    check('promotion confirmed overlap FAIL',"const promotionStatus=!f.exclusions.renewal_loaded?'REVIEW':f.exclusions.promotion_hits.length?'FAIL':'PASS';" in BASE)
    check('facility informational not hard gate',"schemeRow('도시계획시설','현황표시" in BASE and "facilityValue,'INFO'" in BASE)
    check('smallscale official overlap gate',"smallscale_approved:(renewal.loaded&&!renewal.error)?smallscaleHits.length>0:null" in BASE and "const smallscaleStatus=!smallscaleKnown?'REVIEW':f.exclusions.smallscale_approved===true?'FAIL':'PASS';" in BASE)
    ex="const exclusionRows=rows.filter(r=>['정비구역 배타관계','재정비촉진지구','서울도심 특성관리지구','소규모주택정비 인가'].includes(r.item));"
    check('activation exclusion aggregate excludes facility',ex in BASE)
    check('activation popup shows new gates',"'서울도심 특성관리지구','소규모주택정비 인가','배제지역 종합'" in BASE)
    # Scope: derived characteristic dataset must not silently alter Growth Potential module.
    gp=between('growthPotentialSpatialFacts','checkGrowthPotentialFromFacts')
    check('growth module not shared silently','downtown_characteristic' not in gp)
    station_dynamic(); vector_dynamic()
    print('ALL R5 TARGETED CHECKS PASSED')

if __name__=='__main__': main()
