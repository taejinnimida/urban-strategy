from pathlib import Path
import re, subprocess, tempfile, sys
ROOT=Path(__file__).resolve().parent
HTML=(ROOT/'app.html').read_text(encoding='utf-8')
checks=[]
def ck(name, ok):
    ok=bool(ok); checks.append((name,ok)); print(('PASS' if ok else 'FAIL'),name)

# Urban regeneration temporary hold
ck('regen hold feature flag', 'const URBAN_REGENERATION_RESTRICTION_PAUSED=true' in HTML)
ck('regen analysis UI gray class', 'analysis-paused-item' in HTML and '도시재생 관련 지역·지구 <em>잠정 중지</em>' in HTML)
ck('regen manual deemed control disabled', 'id="urban_deemed_regeneration" disabled' in HTML and '도시재생활성화계획 의제경로 해당 · 잠정 중지' in HTML)
fn=HTML[HTML.index('async function analyzeUrbanRegenerationRestrictions()'):HTML.index('async function analyzeSchoolAbsoluteProtection()')]
ck('regen analyzer returns paused before API', "if(URBAN_REGENERATION_RESTRICTION_PAUSED)" in fn and "a.status='paused'" in fn and fn.index("return a;") < fn.index("/api/spatial/land-use-restrictions"))
ck('regen auto pipeline skips API while paused', "markAnalysisProgress('도시재생 관련 지역·지구','partial','잠정 중지 · 사업판정 REVIEW 보류')" in HTML)
pc=HTML[HTML.index('function publicComplexUrbanRegenerationFact'):HTML.index('function publicComplexSpatialFacts')]
ck('public complex regen forced REVIEW', "status:'REVIEW',value:'검토 잠정 중지'" in pc)
ck('public complex regen row marked hold wording', "schemeRow('도시재생 관련 지역·지구 배제'" in HTML and '자동검토 잠정 중지 · 사업판정 보류' in HTML)
auto=HTML[HTML.index('function smallscaleAutonomousTargetFacts'):HTML.index('function smallscaleSpatialFacts')]
ck('autonomous urban regen path suppressed', '!URBAN_REGENERATION_RESTRICTION_PAUSED && c.urbanDeemedRegeneration===true' in auto)
ck('autonomous existing projects suppress regen when paused', 'URBAN_REGENERATION_RESTRICTION_PAUSED?/자율주택|빈집|소규모주택정비/' in auto)

# Smallscale popup 3 actual merge
ck('smallscale source catalog far 2025', 'SMALL_FAR_2025:{' in HTML)
ck('smallscale source catalog current special far', 'SMALL_SPECIAL_FAR:{' in HTML)
ck('smallscale far profile helper', 'function popupSmallscaleFarProfile' in HTML)
ck('smallscale popup3 helper', 'function smallscaleDensityPlanRowsForPopup' in HTML)
for key in ['autonomous','block','reconstruction','redevelopment','moa']:
    ck(f'smallscale popup3 branch {key}', f"key==='{key}'" in HTML[HTML.index('function smallscaleDensityPlanRowsForPopup'):HTML.index('function renewalDensityPlanRowsForPopup')])
renderer=HTML[HTML.index('function renderSmallscaleSchemeDetailPopup'):HTML.index('// ============================================================\n// R40 POPUP-3 PATCH')]
ck('smallscale renderer computes plan rows', 'const planRows=smallscaleDensityPlanRowsForPopup(key,res,store,f);' in renderer)
ck('smallscale renderer has section 3', '<h4>3. 계획기준·용적률·공공부담</h4>' in renderer and '<tbody>${planRows}</tbody>' in renderer)
ck('smallscale popup3 display-only note', '3번 계획기준은 사업방식별 고유 Rule을 표시하며 후보 PASS/FAIL에는 직접 사용하지 않습니다.' in renderer)
ck('smallscale reconstruction 50pct recovery', '법 제49조의2 제3항 + 서울시 조례 제50조의2 제5항' in HTML)
ck('smallscale redevelopment 50pct recovery', '법 제49조의2 제2항 + 서울시 조례 제50조의2 제4항' in HTML)
ck('smallscale autonomous 120pct special', "법적상한용적률의 120%까지 조건부" in HTML)
ck('smallscale block rental special', '공공임대·공공지원민간임대 20% 이상 → 법적상한 / 공공임대 10~20% 미만 → 비례산식' in HTML)
ck('app version retained v2.5.0', 'v2.5.0' in HTML)

# Inline JS syntax
m=re.findall(r'<script(?:\s[^>]*)?>(.*?)</script>',HTML,re.S|re.I)
js='\n\n'.join(x for x in m if x.strip())
with tempfile.NamedTemporaryFile('w',suffix='.js',encoding='utf-8',delete=False) as f:
    f.write(js); name=f.name
r=subprocess.run(['node','--check',name],capture_output=True,text=True)
ck('inline javascript syntax',r.returncode==0)
try: Path(name).unlink()
except: pass

passed=sum(ok for _,ok in checks); total=len(checks)
if passed!=total:
    print(f'R40 REGEN HOLD + SMALLSCALE POPUP3 CHECKS FAILED ({passed}/{total})'); sys.exit(1)
print(f'ALL R40 REGEN HOLD + SMALLSCALE POPUP3 CHECKS PASS ({passed}/{total})')
