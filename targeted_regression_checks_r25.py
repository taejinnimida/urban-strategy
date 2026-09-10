from pathlib import Path
import re, sys
p=Path(__file__).resolve().parent/'app.html'
s=p.read_text(encoding='utf-8')
checks=[]
def ck(name, cond):
    checks.append((name,bool(cond)))

# official block semantics: station catchment / block, not selected site / block
cand=re.search(r'function activationStationCandidates\(\)\{.*?\n\}',s,re.S)
ct=cand.group(0) if cand else ''
ck('activation block uses station catchment relation', "stationBlockRelation(st,threshold,'activation')" in ct)
ck('activation block no selected-site relation in candidate', "schemeStreetBlockRelation('activation')" not in ct)
ck('activation block rule wording station range', '승강장 250/350m 역세권 범위가 해당 가로구역의 1/2 이상' in s)
ck('activation under-half is committee review', "else if(share>0){blockStatus='REVIEW';blockConditional=true" in s)
ck('activation block row remains nonmandatory', bool(re.search(r"schemeRow\('가로구역 포함'.*?stationDecision\.block\.note,false,",s,re.S)))

# road facts should come directly from road engine, with UI fields only as fallback
ck('common scheme road4 engine first', "road4Faces:roadNetNum(roadNet.road4Faces)??schemeNum('scheme_road4_faces')" in s)
ck('common scheme max road engine first', "maxRoad:roadNetNum(roadNet.maxWidth)??schemeNum('scheme_max_road_width')" in s)
ck('compact road facts engine direct', 'const road4=n(net.road4Faces)??c.road4Faces' in s)
ck('activation road direct binary', "roadStatus=(road4Count>=2&&roadHas8===true)?'PASS':'FAIL'" in s)
ck('activation road shows 8m face count', '8m+ ${road8Count!=null?road8Count+\'면\'' in s)
ck('activation road no estimate conditional downgrade', "conditional:roadStatus==='PASS'&&f.road.quality==='ESTIMATE'" not in s)
ck('activation road vehicle access kept as followup', '원활한 차량 진출입 상세계획은 후속 설계·인허가 단계 확인' in s)

# preserve recent platform work
ck('activation station candidate prefers general block path', 'blockRank=x=>{const sh=Number(x?.activation_block?.max_share_pct)' in s)
ck('R21 frontage retained', '4m 기준 미접도율' in s and '6m 기준 미접도율' in s)
ck('R20 prefilters retained', '24000' in s and '36000' in s and '12000' in s)

failed=[n for n,v in checks if not v]
for n,v in checks: print(('PASS' if v else 'FAIL'),n)
if failed:
    print(f'R25 FAILED {len(failed)}/{len(checks)}:',', '.join(failed));sys.exit(1)
print(f'ALL R25 TARGETED CHECKS PASS ({len(checks)}/{len(checks)})')
