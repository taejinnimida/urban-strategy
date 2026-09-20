from pathlib import Path
import hashlib,re,subprocess,sys,tempfile
ROOT=Path(__file__).resolve().parent
html=(ROOT/'app.html').read_text(encoding='utf-8')
checks=[]
def check(name,ok,detail=''):
    checks.append((name,bool(ok),detail));print(('PASS' if ok else 'FAIL'),name,detail)
def extract(src,name):
    m=re.search(r'(?:async\s+)?function\s+'+re.escape(name)+r'\s*\(',src)
    if not m:return None
    st=m.start();b=src.find('{',m.end());depth=0;q=None;esc=False;i=b
    while i<len(src):
        c=src[i]
        if q:
            if esc:esc=False
            elif c=='\\':esc=True
            elif c==q:q=None
        else:
            if c in "'\"`":q=c
            elif c=='{':depth+=1
            elif c=='}':
                depth-=1
                if depth==0:return src[st:i+1]
        i+=1
    return None

check('site card has detail button','openDistrictUnitPlanReviewModal()' in html and '>상세검토</button>' in html)
check('district unit review modal exists','id="districtUnitPlanReviewModal"' in html and 'id="districtUnitPlanReviewBody"' in html)
check('district unit modal renderer exists','function renderDistrictUnitPlanReviewModal' in html and 'CURRENT PLAN · 현행 지구단위계획' in html)
check('district unit modal keeps no-status-impact','사업판정 영향' in html and '지구단위계획 정보만으로 기존 PASS/FAIL/REVIEW를 변경하지 않음' in html)
check('district unit detailed fields stay document review','세부 결정사항' in html and '자료 미연계' in html and '결정도서·시행지침 확인' in html)
check('scheme popup links same review modal',"districtUnitPlanReviewNoteHtml(name)" in html and "openDistrictUnitPlanReviewModal('${safeScheme}')" in html)
check('official source links retained','DISTRICT_UNIT_PLAN_LINKS.uq161' in html and 'DISTRICT_UNIT_PLAN_LINKS.uq165' in html and 'DISTRICT_UNIT_PLAN_LINKS.announcement' in html)
check('arterial UI includes activation special row',"WIDTH_ROAD_SCHEME_KEYS=['activation_arterial','safe','growth','longterm','innovation_growth','innovation_housing']" in html)
check('activation arterial row uses existing route result','function activationArterialSummaryFact' in html and 'a.routeStatus' in html and 'a.path' in html)
check('activation arterial row says width-independent',"contact_value:'도로폭 독립'" in html and '노선형 상업지역' in html)
check('width summary injects UI-only activation fact','const widthFacts={...facts,activation_arterial:activationArterialSummaryFact()}' in html)
check('arterial UI header explains 6 routes','6개 제도 경로 비교 · 활성화는 노선형 상업지역 경로' in html)

expected={
'buildSiteFactStore':'f0223a1e8a2ea50c9a107a838dcd80332bc383fd90c3f5897f98023f1692a2db',
'densityForScheme':'8200438b9fa05b114c025d275167eecba7f447fd7c9812e868562b662245f7a8',
'runAllSchemeChecks':'ea1389e3a751711a373b91db19c856521097697dc9e83eb715e6ab8dfbd8e1db',
'checkActivationFromFacts':'9656bb0b44a514efc40679cf536c34354695b7912056140227e86853581941a2',
'activationArterialRouteDecision':'cbb1a5554cd30f9dac79a07c68db2fea0a33e2aff20188be36752a6d4491bb86',
'schemeRoadEvidenceFacts':'484e33b35204b251d3139ebd7568726d71025a2a0a32ec26d316598c9db17194',
'analyzeSchemeStreetBlocks':'452e520df0492f09772bfab97d0c9bf84620954d787f92472060e3853e14af23',
'analyzeRoadAccess':'747f5d4c0422cca81632ad9fe4f3d57d2e16c219f39f5506bfdf9b2ceaff646c',
'runAllAutoAnalyses':'d7e776b9b6b57b7ce6186186c62ab9b357d9734d5ea93af00cce407e462979ee'}
for fn,h in expected.items():
    body=extract(html,fn) or ''
    got=hashlib.sha256(body.encode()).hexdigest()
    check(f'R9 core preserved: {fn}',got==h,got[:16])

scripts=[]
for m in re.finditer(r'<script(?:\s[^>]*)?>(.*?)</script>',html,re.S|re.I):
    tag=html[m.start():html.find('>',m.start())+1]
    if not re.search(r'\bsrc\s*=',tag,re.I):scripts.append(m.group(1))
with tempfile.NamedTemporaryFile('w',suffix='.js',encoding='utf-8',delete=False) as f:
    f.write('\n;\n'.join(scripts)); tmp=f.name
node=subprocess.run(['node','--check',tmp],capture_output=True,text=True)
check('browser JavaScript syntax',node.returncode==0,node.stderr.strip()[:180])
failed=[x for x in checks if not x[1]]
print(f'\nSUMMARY {len(checks)-len(failed)}/{len(checks)} PASS')
if failed:
    print('FAILED:',[x[0] for x in failed]);sys.exit(1)
