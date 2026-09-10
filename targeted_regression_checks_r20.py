from pathlib import Path
import re
s=Path(__file__).with_name('app.html').read_text(encoding='utf-8')
checks=[]
def ck(name, cond):
    checks.append((name,bool(cond)))
    print(('PASS' if cond else 'FAIL'), name)

ck('smallscale area prefilter 24000', "smallscale:{key:'smallscale',label:'가로주택정비',analysis_area_cut_m2:24000" in s)
ck('activation area prefilter 36000', "activation:{key:'activation',label:'역세권활성화',analysis_area_cut_m2:36000" in s)
ck('station complex area prefilter 12000', "station_complex:{key:'station_complex',label:'역세권복합개발',analysis_area_cut_m2:12000" in s)
ck('growth potential area prefilter 12000', "growth_potential:{key:'growth_potential',label:'성장잠재권',analysis_area_cut_m2:12000" in s)
ck('area prefilter runs before expensive preflight', s.index('const areaSkip=schemeStreetBlockAreaPrefilter(siteAreaM2)') < s.index('const preflight=await streetBlockRuntimePreflight(signal)'))
ck('all oversized avoids spatial fetch', "if(!eligibleKeys.length){" in s and "status:'skipped'" in s and "reason:'면적규모 초과'" in s)
ck('station reuse disabled when its area cut applies', "&& !areaSkip.station_complex".replace(' ','') in s.replace(' ',''))
ck('skipped card reason visible', "미검토 · 면적규모 초과" in s)
ck('progress classifier uses eligible count', "eligible=Number(v?.eligible_count??4)" in s)
ck('redevelopment frontage report retained', "schemeRow('주택접도율'" in s and "f.entry?.frontage?.value||'산정자료 미확보'" in s)

bad=[n for n,ok in checks if not ok]
if bad:
    raise SystemExit(f'R20 checks failed: {bad}')
print(f'ALL R20 TARGETED CHECKS PASS ({len(checks)}/{len(checks)})')
