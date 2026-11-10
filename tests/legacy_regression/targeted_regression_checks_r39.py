from pathlib import Path
import re, subprocess, tempfile, sys

BASE=Path(__file__).resolve().parent
HTML=(BASE/'app.html').read_text(encoding='utf-8')
checks=[]
def ck(name,cond):
    if not cond: raise AssertionError(name)
    checks.append(name); print('PASS',name)

# 1) station complex: hillside is a hard eligibility exclusion when 40m+10deg overlap exists.
ck('R39 station complex explicit 40m+10deg exclusion', "해발고도 40m 이상 + 경사도 10° 이상 구릉지 제외" in HTML)
ck('R39 station complex hill can FAIL', "const hs=h.known?(h.intersects===true?'FAIL':'PASS'):'REVIEW'" in HTML)
ck('R39 station complex hill is required', "schemeRow('구릉지','해발고도 40m 이상 + 경사도 10° 이상 구릉지 제외',hv,hs,hn,true" in HTML)
ck('R39 station complex no stale hill manual followup', '구릉지·저층주거지 인접 수동확인' not in HTML)

# 2) long-term lease: hillside overlap/adjacency = FAIL, committee relief is note only.
ck('R39 longterm hill direct adjacency state', "hillDirect=hillKnown&&store.site.hill.intersects===true" in HTML and 'hillAdjacent=hillKnown&&!hillDirect' in HTML)
ck('R39 longterm hill failure state', "const hillStatus=!hillKnown?'REVIEW':(hillDirect||hillAdjacent?'FAIL':'PASS')" in HTML)
ck('R39 longterm hill required row', "schemeRow('구릉지','전용·제1종일반주거지역, 구릉지 연접부 등 양호한 저층주거지 보전·자연환경 보호가 필요한 지역은 원칙적 제외'" in HTML and "true,{sourceId:'LONGTERM_OP'" in HTML)
ck('R39 longterm committee exception note', '관련 위원회 인정 시 구릉지 조건 포함·완화 가능' in HTML)
ck('R39 longterm no relaxation-to-pass wording', '완화를 전제로 사업가능 판정하지 않음' in HTML)
ck('R39 longterm popup grouping uses hill', "items:['입지 특성화·제외지역','구릉지']" in HTML)
ck('R39 no stale longterm hill followup item', '구릉지 후속확인' not in HTML)

# 3) redevelopment and Moa: hillside remains planning-only, not an eligibility gate.
ck('R39 redevelopment hill in planning FACT', "planning:{density:densityForScheme('redevelopment',c,store),hill:" in HTML)
ck('R39 redevelopment hill INFO nonrequired', "schemeRow('구릉지 계획조건','구릉지는 재개발 사업진입 요건이 아니라 용도지역 상향·경관관리 등 계획조건',hv,'INFO',hn,false" in HTML)
ck('R39 redevelopment detail plan includes hill', "planItems:['구릉지 계획조건','용적률·계획기준']" in HTML)
ck('R39 moa hill planning criterion INFO', "smallscaleCriterion('구릉지 계획조건','구릉지는 사업진입 배제조건이 아니라 관리계획·통합심의의 지형순응·높이·경관 등 계획조건',moaHillValue,'INFO'" in HTML)
ck('R39 moa hill FACT shown in site facts', "spatialFactRow('모아주택 구릉지 계획조건'" in HTML)

# 4) Existing R37 terrain model and R38 road engine must remain present.
ck('R39 R37 terrain model retained', 'safeAnalysisStep(\'구릉지 지형 FACT\',analyzeHillZones' in HTML and '서울시 공식 구릉지 SHP가 아니라' in HTML)
for t in (4,6,8,20,35):
    ck(f'R39 R38 cumulative road threshold {t}m retained', f'road{t}Faces:faceCountAt({t})' in HTML)
ck('R39 no 25m road threshold', 'road25Faces' not in HTML and 'faceCountAt(25)' not in HTML)

# 5) Syntax.
scripts=re.findall(r'<script[^>]*>(.*?)</script>',HTML,re.S|re.I)
with tempfile.NamedTemporaryFile('w',suffix='.js',encoding='utf-8',delete=False) as tf:
    tf.write('\n'.join(scripts)); inline=tf.name
rr=subprocess.run(['node','--check',inline],capture_output=True,text=True)
ck('R39 inline javascript syntax',rr.returncode==0)
if rr.returncode!=0: print(rr.stdout,rr.stderr)
rr=subprocess.run([sys.executable,'-m','py_compile',str(BASE/'app.py')],capture_output=True,text=True)
ck('R39 backend python syntax',rr.returncode==0)
print(f'ALL R39 TARGETED CHECKS PASS ({len(checks)}/{len(checks)})')
