from pathlib import Path
html=Path('app.html').read_text(encoding='utf-8')
checks=[
('4m frontage metric visible','id="spRoadFrontage4Value"' in html),
('4m nonfrontage metric visible','id="spRoadNonFrontage4Value"' in html),
('6m frontage metric visible','id="spRoadFrontage6Value"' in html),
('6m nonfrontage metric visible','id="spRoadNonFrontage6Value"' in html),
('redevelopment 6m direct binary',"const redevelopmentStatus=!redevelopmentKnown?'REVIEW':ratio6<=40?'PASS':'FAIL';" in html),
('redevelopment frontage participates OR','const extraStatuses=[smallStatus,frontageStatus,densityStatus];' in html),
('redevelopment all three fail aggregate',"과소필지·주택접도율·호수밀도 3개 항목 모두 미달" in html),
('resenv general 4m fact',"fact_key:'residentialEnvironmentFrontage4Fact'" in html),
('resenv deadend 6m caveat','연장 35m 이상 막다른 도로는 폭 6m' in html),
('resenv definite pass upper bound','if(ratio4<=20){resenvStatus=\'PASS\'' in html),
('resenv definite fail lower bound',"else if(ratio6>20){resenvStatus='FAIL'" in html),
('resenv frontage participates OR','const extraStatuses=[ageStatus,frontageStatus,smallStatus]' in html),
('scheme road parcel frontage visualization','function schemeRoadParcelEvidenceStyle(f)' in html),
('resenv selectable in frontage map','value="residential_environment">주거환경개선 접도율(4m)' in html),
('road default remains 100m','async function fetchRoadNetwork(radiusM=100){' in html),
('R20 streetblock area prefilter retained',"smallscale:{key:'smallscale',label:'가로주택정비',analysis_area_cut_m2:24000" in html and "activation:{key:'activation',label:'역세권활성화',analysis_area_cut_m2:36000" in html),
]
for name,ok in checks:
    print(('PASS' if ok else 'FAIL'),name)
    if not ok: raise SystemExit(1)
print(f'ALL R21 TARGETED CHECKS PASS ({len(checks)}/{len(checks)})')
