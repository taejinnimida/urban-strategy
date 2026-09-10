from pathlib import Path
html=Path('app.html').read_text(encoding='utf-8')
py=Path('app.py').read_text(encoding='utf-8')
checks=[
('road default 100m','async function fetchRoadNetwork(radiusM=100){' in html),
('streetblock 750 preserved','fetchIndependentRoadFacts(750)' in html),
('activation arterial 220 preserved','independentRoadManageCandidate(search,220)' in html),
('redevelopment frontage report item',"schemeRow('주택접도율',f.entry.frontage?.criterion" in html),
('redevelopment frontage overview actual',"['주택접도율',f.entry?.frontage?.value||'산정자료 미확보']" in html),
('redevelopment frontage nonmandatory',"redevelopmentFrontageDisplayNote,false,{sourceId:'RENEWAL_ORD'" in html),
('actual buffer search','search_metric = site_metric.buffer(float(radius_m))' in py),
('actual buffer road clip','intersection(search_metric)' in py),
]
for name,ok in checks:
    print(('PASS' if ok else 'FAIL'),name)
    if not ok: raise SystemExit(1)
print(f'ALL R19 TARGETED CHECKS PASS ({len(checks)}/{len(checks)})')
