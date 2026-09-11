from pathlib import Path
import re, subprocess, tempfile, textwrap, json

ROOT=Path(__file__).resolve().parent
HTML=(ROOT/'app.html').read_text(encoding='utf-8')
PYCODE=(ROOT/'app.py').read_text(encoding='utf-8')
checks=[]

def ck(name, cond):
    if not cond:
        raise AssertionError(name)
    print('PASS', name); checks.append(name)

# 1. Road Fact -> scheme rule wiring
ck('growth 6m faces engine-first', "growthRoad6Faces:roadNetNum(roadNet.road6Faces)??schemeNum('scheme_growth_road6_faces')" in HTML)
ck('activation 4m 8m direct binary retained', "roadStatus=(road4Count>=2&&roadHas8===true)?'PASS':'FAIL'" in HTML)
ck('station complex 4m 8m direct binary retained', "f.road.road4_faces>=2&&f.road.has8" in HTML)
ck('safe arterial uses total face fact', "const totalFaces=f.road.face_count==null?null:Number(f.road.face_count);" in HTML)
ck('safe arterial uses road6 engine fact', "const road6Faces=f.road.road6_faces==null?null:Number(f.road.road6_faces);" in HTML)
ck('safe one face exception stays review', "접면 1면 + 6m 이상 도로 확보" in HTML and "roadStatus='REVIEW'" in HTML)
ck('safe unknown road candidate stays review', "status:!safeCandidate.known?'REVIEW':safeCandidate.candidate?'CONFIRMED':'FAIL'" in HTML)
ck('public commercial no estimate downgrade', "f.road.quality==='ESTIMATE'?'REVIEW':'PASS'" not in HTML)
ck('public commercial direct road binary', "const roadStatus=rv.some(x=>x==null)?'REVIEW':rok?'PASS':'FAIL';" in HTML)
ck('public housing frontage remains explicit review', "현재 6m 기준 자동산정" in HTML and "PASS/FAIL에 사용하지 않음" in HTML)
ck('public road evidence is subtype-specific', "publicType==='commercial'" in HTML and "주거상업고밀형 4m/8m 기준을 다른 유형에 공용하지 않음" in HTML)

# 2. Station complex hard min/max so near-min shortage can be candidate heuristic, over-max does not become an area adjustment.
ck('station complex below 1500 is fail', "if(f.area.m2<1500){areaStatus='FAIL';areaNote='1,500㎡ 법정 최소면적 미달';}" in HTML)
ck('station complex special 5k to 10k review', "else if(f.area.m2<=10000){areaStatus='REVIEW'" in HTML)
ck('station complex over 10k is fail', "위원회 인정 특례 상한 10,000㎡ 초과" in HTML)

# 3. Area recommendation tolerance source checks
ck('area recommendation tolerance 10 percent', 'const CANDIDATE_AREA_SHORTFALL_TOLERANCE_PCT=10;' in HTML)
ck('area recommendation only below legal minimum', 'if(!Number.isFinite(minimum)||minimum<=0||area>=minimum)continue;' in HTML)
ck('area recommendation 10 percent cap enforced', 'pct<=CANDIDATE_AREA_SHORTFALL_TOLERANCE_PCT+1e-9' in HTML)
ck('large or max area fail blocks adjustment', "if(failedAreaRows.length&&!area)return null;" in HTML)
ck('area does not mask other fail rows', "if(row.hardGate==='AREA')continue;" in HTML and "if(area)labels.push(area.label);" in HTML)
ck('area opportunity label shows m2 and percent', "법정 최소면적까지 ${gap.toLocaleString('ko-KR')}㎡(${pct.toFixed(1)}%) 부족" in HTML)

# Evaluate the actual R26 helper functions in Node for boundary cases.
def extract_function(name):
    marker=f'function {name}'
    start=HTML.index(marker)
    brace=HTML.index('{',start)
    depth=0
    for i in range(brace,len(HTML)):
        ch=HTML[i]
        if ch=='{': depth+=1
        elif ch=='}':
            depth-=1
            if depth==0:
                return HTML[start:i+1]
    raise RuntimeError(name)

min_fn=extract_function('candidateAreaMinimumM2')
opp_fn=extract_function('candidateAreaAdjustmentOpportunity')
node=textwrap.dedent(f"""
const CANDIDATE_AREA_SHORTFALL_TOLERANCE_PCT=10;
let schemeResults={{}};
let latestSiteFactStore={{common:{{area:null}}}};
function commonSchemeData(){{return {{area:latestSiteFactStore.common.area}};}}
{min_fn}
{opp_fn}
function row(){{return {{status:'FAIL',evaluatedStatus:'FAIL',hardGate:'AREA',rule:'5,000㎡ 이상',item:'면적'}};}}
function run(name,area,facts={{}}){{
  schemeResults[name]={{facts:Object.assign({{area:{{m2:area}}}},facts),rows:[row()]}};
  latestSiteFactStore.common.area=area;
  return candidateAreaAdjustmentOpportunity(name,{{areaReason:''}});
}}
const out={{
 prior4600:run('prior_negotiation',4600),
 prior4500:run('prior_negotiation',4500),
 prior4499:run('prior_negotiation',4499),
 prior4000:run('prior_negotiation',4000),
 innovation19000:run('innovation_housing',19000),
 innovation10000:run('innovation_housing',10000),
 innovation65000:run('innovation_housing',65000),
 public9500:run('public_complex',9500,{{type:'housing'}}),
 public8000:run('public_complex',8000,{{type:'housing'}}),
 redevelopment4600:run('redevelopment',4600),
 safe4500:run('safe',4500,{{area:{{m2:4500,min_m2:5000}}}})
}};
console.log(JSON.stringify(out));
""")
with tempfile.NamedTemporaryFile('w',suffix='.js',encoding='utf-8',delete=False) as f:
    f.write(node); path=f.name
proc=subprocess.run(['node',path],capture_output=True,text=True,check=True)
out=json.loads(proc.stdout.strip())
ck('area 8 percent shortage adjustable', out['prior4600'] is not None and abs(out['prior4600']['shortfall_pct']-8)<1e-6)
ck('area exactly 10 percent shortage adjustable', out['prior4500'] is not None and abs(out['prior4500']['shortfall_pct']-10)<1e-6)
ck('area over 10 percent shortage not adjustable', out['prior4499'] is None and out['prior4000'] is None)
ck('innovation housing 5 percent shortage adjustable', out['innovation19000'] is not None and abs(out['innovation19000']['shortfall_pct']-5)<1e-6)
ck('innovation housing huge shortage not adjustable', out['innovation10000'] is None)
ck('maximum area excess not converted to adjustment', out['innovation65000'] is None)
ck('public housing 5 percent shortage adjustable', out['public9500'] is not None and abs(out['public9500']['shortfall_pct']-5)<1e-6)
ck('public housing 20 percent shortage not adjustable', out['public8000'] is None)
ck('redevelopment uses 5000 exception floor', out['redevelopment4600'] is not None and abs(out['redevelopment4600']['shortfall_pct']-8)<1e-6)
ck('dynamic scheme minimum overrides fixed fallback', out['safe4500'] is not None and abs(out['safe4500']['minimum_m2']-5000)<1e-6)

# Candidate recommendation must not let a near-area adjustment hide another clear FAIL.
change_fn=extract_function('candidateChangeOpportunity')
node2=textwrap.dedent(f"""
const CANDIDATE_AREA_SHORTFALL_TOLERANCE_PCT=10;
let schemeResults={{}}; let latestSiteFactStore={{common:{{area:null}}}};
const SHELL_SCHEMES=new Set();
function commonSchemeData(){{return {{area:latestSiteFactStore.common.area}};}}
function candidateOneYearAgeOpportunity(){{return null;}}
{min_fn}
{opp_fn}
{change_fn}
const areaRow={{item:'면적',rule:'5,000㎡ 이상',status:'FAIL',evaluatedStatus:'FAIL',required:true,hardGate:'AREA',note:'',gap:'400㎡ 부족'}};
const zoneRow={{item:'용도지역',rule:'준주거',status:'FAIL',evaluatedStatus:'FAIL',required:true,hardGate:null,note:'',gap:''}};
const st={{ready:true,purposeGate:'priority',structural:'PASS',stage:'AREA',execution:{{hardMismatch:false}}}};
schemeResults.prior_negotiation={{facts:{{area:{{m2:4600}}}},rows:[areaRow]}}; latestSiteFactStore.common.area=4600;
const areaOnly=candidateChangeOpportunity('prior_negotiation',st);
schemeResults.prior_negotiation={{facts:{{area:{{m2:4600}}}},rows:[areaRow,zoneRow]}};
const areaPlusZone=candidateChangeOpportunity('prior_negotiation',st);
console.log(JSON.stringify({{areaOnly,areaPlusZone}}));
""")
with tempfile.NamedTemporaryFile('w',suffix='.js',encoding='utf-8',delete=False) as f:
    f.write(node2); path2=f.name
proc2=subprocess.run(['node',path2],capture_output=True,text=True,check=True)
out2=json.loads(proc2.stdout.strip())
ck('near-area-only fail can stay adjustable', out2['areaOnly'] is not None and out2['areaOnly']['kind']=='AREA')
ck('near area does not hide unrelated zoning fail', out2['areaPlusZone'] is None)

# Prior performance/UI layers retained
ck('R23 neighbor graph retained', 'neighbor_topology_ms' in PYCODE and '_neighbor_topology' in PYCODE)
ck('R24 parcel click UI retained', "let parcelFindMode='input'" in HTML and 'function setParcelFindMode(mode)' in HTML and 'applySelectedParcelsAsBoundary()' in HTML)
ck('R25 activation station block relation retained', "stationBlockRelation(st,threshold,'activation')" in HTML or "stationBlockRelation(st, threshold, 'activation')" in HTML)
ck('R21 frontage retained', 'frontage_access_buildings_4m' in HTML and 'residentialEnvironmentFrontage4Fact' in HTML)
ck('R20 area prefilter retained', 'streetblock-area-prefilter' in HTML or '24000' in HTML and '36000' in HTML)
print(f'ALL R26 TARGETED CHECKS PASS ({len(checks)}/{len(checks)})')
