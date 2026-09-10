from pathlib import Path
import sys
p=Path(__file__).with_name('app.html')
s=p.read_text(encoding='utf-8')
checks={
'R16 REVIEW display preserved': "function schemeSheetFeasibility(status,conditional=false)" in s and "return '확인필요';" in s,
'R16 activation route availability preserved': 'activationRouteAlternativeStatus' in s and 'facts.route.available' in s,
'R16 smallscale street-block logic preserved': 'block_coverage_of_site_pct' in s and 'site_share_of_block_pct' in s,
'longterm age resolver linked': "resolvedSchemeAgeFact(store,'longterm',route)" in s,
'longterm planned units neutral': "'계획 세대수','100세대 이상은 사업계획 수립·규모계획 단계에서 확보'" in s and "'INFO',f.units.note,false" in s,
'longterm consent neutral': s.count("rows.push(schemeRow('사업추진 동의'") >= 2,
'longterm API-linked exclusions split': 'const autoKnown=ev.zoning_known&&ev.landscape_known&&ev.heritage_known' in s,
'longterm hill follow-up separated': "'구릉지 후속확인'" in s,
'public complex age resolver linked': "resolvedSchemeAgeFact(store,'public_complex')" in s,
'public complex 3 route evaluator': 'function evaluatePublicComplexRoutes(store,facts)' in s,
'public complex 3 route popup tabs': 'selectPublicComplexPopupRoute' in s and "['commercial','industrial','housing']" in s,
'public complex common exclusions split': all(x in s for x in ["'정비구역 배제'","'도시개발구역 배제'","'도시재생사업 인허가 배제'"]),
'public complex consent not hard gate': "'주민동의율·사업추진성'" in s and "'INFO'" in s,
'site analysis urban-regeneration permit status visible': 'spDevelopmentRegeneration' in s,
'urban regeneration activation-area not misused as permit': "도시재생활성화지역 지정만으로 배제하지 않고" in s,
}
fail=[]
for name,ok in checks.items():
    print(('PASS' if ok else 'FAIL'), name)
    if not ok: fail.append(name)
if fail:
    sys.exit(1)
print(f'ALL R17 TARGETED CHECKS PASS ({len(checks)}/{len(checks)})')
