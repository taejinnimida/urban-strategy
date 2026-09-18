from pathlib import Path
import hashlib, importlib.util, json, re, subprocess, sys, tempfile
from shapely.geometry import shape
from shapely.strtree import STRtree
from pyproj import Transformer
from shapely.ops import transform as geometry_transform

ROOT=Path(__file__).resolve().parent
html=(ROOT/'app.html').read_text(encoding='utf-8')
py=(ROOT/'app.py').read_text(encoding='utf-8')
checks=[]

def check(name, ok, detail=''):
    checks.append((name,bool(ok),detail))
    print(('PASS' if ok else 'FAIL'),name,detail)

def extract_function(src,name):
    m=re.search(r'(?:async\s+)?function\s+'+re.escape(name)+r'\s*\(',src)
    if not m:return None
    st=m.start(); b=src.find('{',m.end()); depth=0; quote=None; esc=False; i=b
    while i<len(src):
        c=src[i]
        if quote:
            if esc: esc=False
            elif c=='\\': esc=True
            elif c==quote: quote=None
        else:
            if c in "'\"`": quote=c
            elif c=='{': depth+=1
            elif c=='}':
                depth-=1
                if depth==0:return src[st:i+1]
        i+=1
    return None

# --- urban-regeneration R3 baseline preservation ---
check('BZ604 mapping removed', "'BZ604': ('other', '도시재생활성화지역')" not in py)
check('legacy urban-regeneration GeoJSON absent', 'urban_regeneration_innovation_reference.geojson' not in py and 'urban_regeneration_innovation_reference.geojson' not in html)
check('legacy urban-regeneration endpoint absent', '/api/reference/urban-regeneration-innovation' not in py and '/api/reference/urban-regeneration-innovation' not in html)
required=[ROOT/f'urban_regeneration_innovation_gimpo{ext}' for ext in ('.shp','.shx','.dbf','.prj','.cpg')]
check('Gimpo innovation SHP set complete', all(p.exists() for p in required))

spec=importlib.util.spec_from_file_location('platform_app_r4',ROOT/'app.py')
mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
fc=mod._urban_regen_innovation_project_features(); meta=fc.get('metadata') or {}
check('Gimpo innovation SHP backend ready',mod._urban_regen_innovation_shp_ready())
check('Gimpo innovation SHP feature count=1',len(fc.get('features') or [])==1,str(len(fc.get('features') or [])))
check('Gimpo innovation SHP CRS EPSG:5181',meta.get('source_crs')=='EPSG:5181',str(meta.get('source_crs')))
area=float(meta.get('source_area_m2') or 0)
check('Gimpo innovation SHP area stable',abs(area-359275.76)<1.0,f'{area:.2f} m2')

# --- raw legal/rule modules must remain byte-identical to the verified R3 baseline ---
EXPECTED={
 'checkActivationFromFacts':'9656bb0b44a514efc40679cf536c34354695b7912056140227e86853581941a2',
 'priorNegotiationSpatialFacts':'1595572e0678f49e2288cace4557952fca201556e489da28cccaddeadd0053ce',
 'checkPriorNegotiationFromFacts':'fb89d91796ef82ef62876721a28db4a59533a8869fad2e3a25fadc6378684de0',
 'densityForScheme':'8200438b9fa05b114c025d275167eecba7f447fd7c9812e868562b662245f7a8',
 'buildSiteFactStore':'f0223a1e8a2ea50c9a107a838dcd80332bc383fd90c3f5897f98023f1692a2db',
 'analyzeSchemeStreetBlocks':'452e520df0492f09772bfab97d0c9bf84620954d787f92472060e3853e14af23',
 'analyzeRoadAccess':'747f5d4c0422cca81632ad9fe4f3d57d2e16c219f39f5506bfdf9b2ceaff646c',
 'runAllAutoAnalyses':'d7e776b9b6b57b7ce6186186c62ab9b357d9734d5ea93af00cce407e462979ee'
}
for fn,h in EXPECTED.items():
    x=extract_function(html,fn); now=hashlib.sha256((x or '').encode()).hexdigest() if x else ''
    check(f'raw/core function preserved: {fn}',now==h,now[:16])

# --- strategic competition layer ---
check('competition panel inserted','id="schemeCompetitionPanel"' in html and '사업성 제고제도 경합' in html)
check('competition state isolated','let schemeCompetitionState=' in html and 'analyzeActivationPriorNegotiationCompetition(store)' in html)
check('runAll computes competition after rule modules', 'moduleErrors.push(...evaluateSchemeModulesSafely(store));\n    // 2-1)' in html and 'schemeCompetitionState=analyzeActivationPriorNegotiationCompetition(store);' in html)
check('raw result not overwritten','schemeResults.activation.overall=' not in html and 'schemeResults.prior_negotiation.overall=' not in html)
check('competition display uses red adjustable class',"label:'경합검토'" in html and "label:'추진후보'" in html and "css:'candidate-adjustable'" in html)
check('activation route UI surfaced','activationRouteUiSummary()' in html and '간선가로형' in html and '노선형 상업지역 포함' in html)
check('one-parcel under-5000 scenario present','selectedParcelUnder5000Scenario()' in html and '1필지 제외 시 5,000㎡ 미만 조정 가능한 조합' in html)
check('competition retained in recommendations',"['available','alternative','adjustable'].includes(display.kind)" in html and 'display.competition' in html)
check('AI receives competition FACT','scheme_competition:schemeCompetitionState?.relation_active' in html and 'strategic_competition:display.competition===true' in html)

# Verify the strategy layer does not rescue a real independent activation FAIL.
start=html.index('let schemeCompetitionState='); end=html.index('function runAllSchemeChecks(){',start)
module_js=html[start:end]
unit_js=f"""
const vm=require('vm');
const context={{
 selectedParcelPnus:new Set(['1','2','3']),
 parcelFeatureMap:new Map([['1',{{properties:{{official_area_m2:2000,jibun:'A'}}}}],['2',{{properties:{{official_area_m2:1600,jibun:'B'}}}}],['3',{{properties:{{official_area_m2:1493,jibun:'C'}}}}]]),
 turf:{{area:()=>0,feature:g=>({{geometry:g}})}},schemeResults:{{}},document:{{getElementById:()=>null}},escHtml:x=>String(x),console
}};
vm.createContext(context);vm.runInContext({json.dumps(module_js)},context);
const row=(item,status,required=true,value='')=>({{item,status,required,value}});
function base(manual=null,actFail=false,priorFail=false){{
 context.schemeResults={{activation:{{rows:[row('입지유형','PASS'),row('대상지 면적','PASS'),row('사전협상 대상여부','REVIEW'),...(actFail?[row('도로','FAIL')]:[])],facts:{{location:{{station:{{distance_m:161,name:'삼성역'}},arterial:{{linear_commercial:false}}}}}}}},prior_negotiation:{{rows:[row('협상대상지 면적','PASS'),row('협상대상지·부지성격',priorFail?'FAIL':'REVIEW'),row('도시계획변경·협상 필요성','REVIEW')]}}}};
 return {{common:{{area:5093,priorNegotiation:manual}}}};
}}
let x=context.analyzeActivationPriorNegotiationCompetition(base());
if(!(x.relation_active&&x.activation_candidate&&x.prior_candidate&&x.parcel_adjustment.possible))process.exit(11);
x=context.analyzeActivationPriorNegotiationCompetition(base(null,true,false));if(!(x.relation_active&&!x.activation_candidate&&x.prior_candidate))process.exit(12);
x=context.analyzeActivationPriorNegotiationCompetition(base(true,false,false));if(x.relation_active)process.exit(13);
x=context.analyzeActivationPriorNegotiationCompetition({{common:{{area:4900,priorNegotiation:null}}}});if(x.relation_active)process.exit(14);
console.log('competition-unit-pass');
"""
unit=ROOT/'_competition_unit_tmp.js';unit.write_text(unit_js,encoding='utf-8')
node_unit=subprocess.run(['node',str(unit)],capture_output=True,text=True);unit.unlink(missing_ok=True)
check('competition unit scenarios',node_unit.returncode==0,(node_unit.stdout+node_unit.stderr).strip()[:300])

# Retry-13 agreed behavior retained.
check('retry13 one-time sequential','for(const label of retryLabels)' in html and 'while(retryLabels.size>0' not in html and 'Promise.all(current.map' not in html)
check('retry13 excludes no-data states',"['NO_DATA','UNKNOWN','NOT_IMPLEMENTED']" in html)

# Browser JS syntax: extract inline scripts fresh.
try:
    from bs4 import BeautifulSoup
    soup=BeautifulSoup(html,'html.parser')
    inline='\n'.join((x.string if x.string is not None else x.get_text()) for x in soup.find_all('script') if not x.get('src'))
    tmp=ROOT/'_inline_tmp.js';tmp.write_text(inline,encoding='utf-8')
    node=subprocess.run(['node','--check',str(tmp)],capture_output=True,text=True);tmp.unlink(missing_ok=True)
    check('browser JavaScript syntax',node.returncode==0,node.stderr.strip()[:200])
except Exception as e:
    check('browser JavaScript syntax',False,str(e))
check('app.py compile',subprocess.run([sys.executable,'-m','py_compile',str(ROOT/'app.py')],capture_output=True).returncode==0)

failed=[x for x in checks if not x[1]]
print(f'\nSUMMARY {len(checks)-len(failed)}/{len(checks)} PASS')
if failed:
    print('FAILED:',[x[0] for x in failed]);sys.exit(1)
