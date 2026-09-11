from pathlib import Path
import re, subprocess, tempfile

html=Path('app.html').read_text(encoding='utf-8')
checks=[]
def check(name, cond):
    if not cond:
        raise AssertionError(name)
    print('PASS',name); checks.append(name)

# 1. Semiindustrial review panel is visually after every ordinary scheme family and before AI.
pos_icons=html.find('<div class="cc-scheme-icons" id="ccSchemeIcons">')
pos_last_special=html.find('data-scheme="mixed_use_zone"')
pos_semi=html.find('<section class="semiindustrial-review" id="semiindustrialReviewPanel">')
pos_ai=html.find('<section class="ai-comprehensive-panel" id="aiComprehensivePanel"')
check('semiindustrial panel exists', pos_semi>=0)
check('semiindustrial panel is below all normal scheme cards', pos_icons < pos_last_special < pos_semi)
check('semiindustrial panel remains above AI analysis', pos_semi < pos_ai)

# 2. Innovation-housing station fact is independent from legal factory ratio in semiindustrial zone.
check('innovation housing station criterion excludes factory ratio', "'일반지역: 사업면적 과반 역세권 / 준공업: 사업면적 전체 역세권'" in html)
check('old combined station factory criterion removed', '준공업: 사업면적 전체 역세권 + 법정 공장비율 10% 미만' not in html)
check('station value no longer appends factory ratio', "` / 공장 ${fmtSchemePct(f.zoning.factory_pct)}`" not in html)
check('semiindustrial station 350 full coverage passes independently', "f.station.coverage350_pct!=null&&f.station.coverage350_pct>=99.999)loc='PASS'" in html)
check('semiindustrial station 500 full coverage remains market review', "f.station.coverage500_pct!=null&&f.station.coverage500_pct>=99.999){loc='REVIEW';note='350~500m 구간은 시장 인정 필요'" in html)
check('factory ratio is separate mandatory row', "schemeRow('공장비율','준공업지역: 법정 공장비율 10% 미만'" in html)
check('factory row unknown remains review', "const factoryStatus=f.zoning.factory_pct==null?'REVIEW':f.zoning.factory_pct<10?'PASS':'FAIL'" in html)
check('factory row is explicitly separate station hardgate', '역세권 판정과 별도 하드게이트' in html)

# 3. Station summary table uses the station-only row and dynamic route label.
check('station summary still picks innovation housing entry row', "case 'innovation_housing': return pick('입지');" in html)
check('innovation housing route label helper added', 'function stationSchemeRouteLabel(name)' in html)
check('semiindustrial route label says full inclusion', "?'350/500m 대상지 전체포함':'350/500m 대상지 면적 50% 이상'" in html)
check('station renderer uses dynamic route label helper', 'const route=stationSchemeRouteLabel(name);' in html)

# 4. All other R31/R30 structural anchors remain.
check('R31 industrial and factory card retained', '<b>산업 및 공장비율</b>' in html)
check('R31 semiindustrial industrial schemes retained', "title:'산업혁신구역'" in html and "title:'산업정비구역'" in html)
check('R31 industrial park official layer retained', 'LT_C_DAMDAN' in Path('app.py').read_text(encoding='utf-8'))
check('R30 consent and plan policies retained', '사업추진 전제조건' in html and '계획반영필요' in html)
check('R29 project registry retained', 'projectRegistryOverlaps' in html)
check('R28 performance diagnostic retained', 'frontend_postprocess_ms' in html)
check('R27 candidate alternative retained', '대안추천' in html)

# 5. Source-level branch smoke: factory unknown cannot be referenced in station-status branch before separate row.
# Restrict to housing branch text only.
start=html.find("  }else{\n    let loc='REVIEW',note='';\n    const factory=f.zoning.factory;")
end=html.find("    rows.push(schemeRow('노후도'", start)
branch=html[start:end]
check('innovation housing branch located', start>=0 and end>start)
station_part=branch[:branch.find("    if(f.zoning.current==='준공업'){\n      const factoryStatus")]
check('station status branch does not read factory_pct', 'f.zoning.factory_pct' not in station_part)
check('separate factory branch reads factory_pct', 'f.zoning.factory_pct' in branch[len(station_part):])

# Syntax check inline JS.
blocks=re.findall(r'<script[^>]*>(.*?)</script>',html,re.S|re.I)
with tempfile.NamedTemporaryFile('w',suffix='.js',encoding='utf-8',delete=False) as tf:
    tf.write('\n'.join(blocks)); js_path=tf.name
r=subprocess.run(['node','--check',js_path],capture_output=True,text=True)
if r.returncode!=0:
    raise AssertionError('inline JS syntax: '+r.stdout+r.stderr)
check('inline javascript syntax', True)

print(f'ALL R32 TARGETED CHECKS PASS ({len(checks)}/{len(checks)})')
