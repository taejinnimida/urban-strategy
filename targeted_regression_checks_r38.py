from pathlib import Path
import re, subprocess, tempfile, sys, json

BASE=Path(__file__).resolve().parent
HTML=(BASE/'app.html').read_text(encoding='utf-8')
checks=[]
def ck(name,cond):
    if not cond: raise AssertionError(name)
    checks.append(name);print('PASS',name)

# R38 contract: exclusive visual bands, cumulative threshold facts.
ck('R38 independent frontage face helper','function buildFrontageFacesFromAcceptedSegments' in HTML and 'function buildRoadFrontageFaces' in HTML)
ck('R38 face count uses geometric faces','faceCount:frontageFaces.length' in HTML)
for t in (4,6,8,20,35):
    ck(f'R38 cumulative threshold {t}m',f'road{t}Faces:faceCountAt({t})' in HTML)
ck('R38 no 25m face threshold','road25Faces' not in HTML and 'faceCountAt(25)' not in HTML)
ck('R38 road-name records retained for distinct-road rules','const roadSummary=touchedGroups.map' in HTML and 'growthInnovationRoadFactFromRecords(roadSummary.map' in HTML)
ck('R38 frontend table uses frontage faces','const rows=(net.frontageFaces||[]).map' in HTML)
ck('R38 20m and 35m cumulative metrics visible','id="rc20Faces"' in HTML and 'id="rc35Faces"' in HTML and 'id="rc20Contact"' in HTML and 'id="rc35Contact"' in HTML)
for txt in ('35m 이상','20~35m','8~20m','6~8m','4~6m','4m 미만'):
    ck('R38 legend '+txt,txt in HTML)
for marker in ('ccRoadConditionFrontageUnder4','ccRoadConditionFrontage4','ccRoadConditionFrontage6','ccRoadConditionFrontage8','ccRoadConditionFrontage20','ccRoadConditionFrontage35'):
    ck('R38 visual layer '+marker,marker in HTML)
ck('R38 cadastral road remains diagnostic only','현 판정 미반영' in HTML and "sourceType:'CADASTRAL_ROAD'" in HTML)
ck('R38 activation formula preserved',"const activationOk=activationKnown&&Number(c.road4Faces)>=2&&c.has8===true;" in HTML)
ck('R38 R37 hillside retained','id="siteDetail_hill"' in HTML and 'HILL_REFERENCE_ELEVATION_M' not in HTML)  # backend constant is in app.py, hill UI must remain
ck('R38 route commercial retained','MODEL_REFERENCE · 노선형 상업지역 판정용 참조도형' in HTML)

# Dynamic unit test for face splitting/merging and cumulative thresholds.
start=HTML.index('function frontageBearing(a,b)')
end=HTML.index('function buildRoadFrontageFaces(site,roads',start)
helper=HTML[start:end]
js=r'''
const turf={
  point:(c)=>({geometry:{coordinates:c}}),
  lineString:(c,p={})=>({type:'Feature',geometry:{type:'LineString',coordinates:c},properties:p}),
  bearing:(a,b)=>{const [x1,y1]=a.geometry.coordinates,[x2,y2]=b.geometry.coordinates;return Math.atan2(x2-x1,y2-y1)*180/Math.PI;},
  distance:(a,b)=>{const [x1,y1]=a.geometry.coordinates,[x2,y2]=b.geometry.coordinates;return Math.hypot(x2-x1,y2-y1)/1000;},
  length:(f)=>{const c=f.geometry.coordinates;let m=0;for(let i=1;i<c.length;i++)m+=Math.hypot(c[i][0]-c[i-1][0],c[i][1]-c[i-1][1]);return m/1000;}
};
'''+helper+r'''
function seg(coords,width,start,end,name='same-road',order=0){return turf.lineString(coords,{_width_m:width,_frontage_contact_m:end-start,_frontage_ring_index:0,_frontage_order:order,_frontage_start_m:start,_frontage_end_m:end,_frontage_road_name:name,_frontage_key:'name:'+name});}
function counts(faces){let o={};for(const t of [4,6,8,20,35])o[t]=faces.filter(f=>Number(f.threshold_contact[t]||0)>=.5).length;return o;}
// Same road name, but a 90-degree site corner: must be two frontage faces.
let a=seg([[0,0],[10,0]],34,0,10,'same-road',0),b=seg([[10,0],[10,8]],6,10,18,'same-road',1);
let r=buildFrontageFacesFromAcceptedSegments([a,b]);if(r.faces.length!==2)throw new Error('corner split expected 2 faces, got '+r.faces.length);
let c=counts(r.faces);if(JSON.stringify(c)!==JSON.stringify({4:2,6:2,8:1,20:1,35:0}))throw new Error('cumulative counts wrong '+JSON.stringify(c));
// Collinear fragments with different widths are one geometric face, but threshold contacts remain cumulative by actual width.
a=seg([[0,0],[10,0]],34,0,10,'same-road',0);b=seg([[10,0],[20,0]],6,10,20,'same-road',1);
r=buildFrontageFacesFromAcceptedSegments([a,b]);if(r.faces.length!==1)throw new Error('collinear merge expected 1 face, got '+r.faces.length);
c=counts(r.faces);if(JSON.stringify(c)!==JSON.stringify({4:1,6:1,8:1,20:1,35:0}))throw new Error('collinear cumulative wrong '+JSON.stringify(c));
// 35m+ qualifies every lower threshold.
a=seg([[0,0],[12,0]],40,0,12,'wide-road',0);r=buildFrontageFacesFromAcceptedSegments([a]);c=counts(r.faces);if(JSON.stringify(c)!==JSON.stringify({4:1,6:1,8:1,20:1,35:1}))throw new Error('35m cumulative wrong '+JSON.stringify(c));
console.log('FACE_UNIT_OK');
'''
with tempfile.NamedTemporaryFile('w',suffix='.js',encoding='utf-8',delete=False) as tf:
    tf.write(js);unit=tf.name
rr=subprocess.run(['node',unit],capture_output=True,text=True)
ck('R38 dynamic face/cumulative unit test',rr.returncode==0 and 'FACE_UNIT_OK' in rr.stdout)
if rr.returncode!=0: print(rr.stdout,rr.stderr)

# Syntax and backend regression readiness.
scripts=re.findall(r'<script[^>]*>(.*?)</script>',HTML,re.S|re.I)
with tempfile.NamedTemporaryFile('w',suffix='.js',encoding='utf-8',delete=False) as tf:
    tf.write('\n'.join(scripts));inline=tf.name
rr=subprocess.run(['node','--check',inline],capture_output=True,text=True)
ck('R38 inline javascript syntax',rr.returncode==0)
rr=subprocess.run([sys.executable,'-m','py_compile',str(BASE/'app.py')],capture_output=True,text=True)
ck('R38 backend python syntax',rr.returncode==0)
print(f'ALL R38 TARGETED CHECKS PASS ({len(checks)}/{len(checks)})')
