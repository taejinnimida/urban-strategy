from pathlib import Path
import re, subprocess, tempfile

base=Path(__file__).resolve().parent
html=(base/'app.html').read_text(encoding='utf-8')
checks=[]

def check(name,cond):
    if not cond: raise AssertionError(name)
    print('PASS',name);checks.append(name)

# R36 is visualization-only: no rule formulas changed.
check('R36 frontage helper exists','function frontageBoundarySegments' in html)
check('R36 cadastral road candidate helper exists','function roadConditionCadastralRoadFeatures' in html)
check('R36 visual fact helper exists','function roadConditionFrontageVisualFacts' in html)
for marker in ('ccRoadConditionCadastralRoads','ccRoadConditionCenterlines','ccRoadConditionCadFrontage','ccRoadConditionFrontageUnder4','ccRoadConditionFrontage4','ccRoadConditionFrontage8'):
    check('R36 map layer '+marker, marker in html)
check('R36 legend exposes 8m frontage','8m+ 인정 접도' in html)
check('R36 legend exposes 4-8m frontage','4~8m 인정 접도' in html)
check('R36 legend exposes under4 frontage','4m 미만 접도' in html)
check('R36 legend exposes cadastral diagnostic','지목 도로 접면후보' in html)
check('R36 cadastral diagnostic explicitly non-rule','현 판정 미반영' in html)
check('R36 mismatch diagnostic message','지적상 도로와 현행 중심선 버퍼 판정의 불일치 후보' in html)

# Core R35/R26 decision formulas remain present and unchanged in intent.
check('activation still uses 4m two faces plus 8m one face',"const activationOk=activationKnown&&Number(c.road4Faces)>=2&&c.has8===true;" in html)
check('site road face count formula retained',"const faceCountAt=t=>touchedGroups.filter(g=>Number(g.thresholdContact?.[t]?.total_m||0)>=0.5).length;" in html)
check('road centerline width buffer formula retained',"const buffered=turf.buffer(mf,w/2,{units:'meters',steps:8});" in html)
check('frontage sample formula retained',"const step=0.5;" in html and "turf.along(line,(d+seg/2)/1000" in html)
check('route commercial R35 retained','MODEL_REFERENCE · 노선형 상업지역 판정용 참조도형' in html)

# Ensure cadastral road is diagnostic only, not wired into siteRoadNetworkStats or activation rule.
def block(start,end):
    a=html.index(start);b=html.index(end,a);return html[a:b]
site_stats=block('function siteRoadNetworkStats(site,roads){','function setRoadNetworkAutoValue')
activation=block('function schemeFrontageEvidenceFacts','function schemeRoadEvidenceFacts')
check('siteRoadNetworkStats does not use cadastral diagnostic','CADASTRAL_ROAD' not in site_stats and 'roadConditionCadastralRoadFeatures' not in site_stats)
check('activation rule does not use cadastral diagnostic','CADASTRAL_ROAD' not in activation and 'roadConditionCadastralRoadFeatures' not in activation)

# Syntax.
blocks=re.findall(r'<script[^>]*>(.*?)</script>',html,re.S|re.I)
with tempfile.NamedTemporaryFile('w',suffix='.js',encoding='utf-8',delete=False) as tf:
    tf.write('\n'.join(blocks));js=tf.name
r=subprocess.run(['node','--check',js],capture_output=True,text=True)
check('inline javascript syntax',r.returncode==0)
r=subprocess.run(['python','-m','py_compile',str(base/'app.py')],capture_output=True,text=True)
check('backend python syntax',r.returncode==0)

print(f'ALL R36 TARGETED CHECKS PASS ({len(checks)}/{len(checks)})')
