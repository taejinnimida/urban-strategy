from pathlib import Path
import hashlib, re, subprocess, tempfile, textwrap

ROOT=Path(__file__).resolve().parent
APP=ROOT/'app.html'
BASE=APP.read_text(encoding='utf-8')

def check(name, cond, detail=''):
    if not cond:
        raise AssertionError(f'{name}: {detail or "FAILED"}')
    print(f'PASS {name}')

def extract_function(name):
    # supports function / async function declarations
    m=re.search(rf'(?:async\s+)?function\s+{re.escape(name)}\s*\(', BASE)
    if not m: raise AssertionError(f'function not found: {name}')
    start=m.start(); body_pair=BASE.find('){',m.end()); brace=body_pair+1 if body_pair>=0 else BASE.find('{',m.end())
    depth=0; quote=None; esc=False; template_depth=0
    for i in range(brace,len(BASE)):
        ch=BASE[i]
        if quote:
            if esc: esc=False; continue
            if ch=='\\': esc=True; continue
            if ch==quote: quote=None
            continue
        if ch in ('\"',"'",'`'):
            quote=ch; continue
        if ch=='{': depth+=1
        elif ch=='}':
            depth-=1
            if depth==0:return BASE[start:i+1]
    raise AssertionError(f'unbalanced function: {name}')

def dynamic_js_checks():
    f1=extract_function('activationArterialRouteDecision')
    f2=extract_function('activationStationCriterion')
    js=f'''{f1}\n{f2}\n
function assert(c,m){{if(!c)throw new Error(m);}}
(async()=>{{
  // mock reject: Promise.allSettled의 rejected 상태를 route helper에 전달
  const settled=await Promise.allSettled([Promise.reject(new Error('zone down')),Promise.reject(new Error('road down'))]);
  let r=activationArterialRouteDecision({{zoneFetchFailed:settled[0].status!=='fulfilled',roadFetchFailed:settled[1].status!=='fulfilled',roadCount:0,zoneCount:0}});
  assert(r.status==='REVIEW','mock reject must REVIEW');
  r=activationArterialRouteDecision({{roadFetchFailed:false,zoneFetchFailed:false,roadCount:0,zoneCount:2}});
  assert(r.status==='FAIL','normal road 0 must FAIL');
  r=activationArterialRouteDecision({{roadFetchFailed:false,zoneFetchFailed:false,roadCount:2,zoneCount:0}});
  assert(r.status==='FAIL','normal zoning 0 must FAIL');
  r=activationArterialRouteDecision({{blockIncludes:true,roadFetchFailed:true,zoneFetchFailed:true}});
  assert(r.status==='PASS','confirmed block should remain PASS');

  let s=activationStationCriterion({{threshold_m:250,threshold_status:'CONFIRMED',distance_m:4.6,block_share_pct:null}},{{}});
  assert(s.direct.status==='PASS','4.6m direct PASS');
  assert(s.block.status==='REVIEW','unknown block remains REVIEW');
  assert(s.overall.status==='PASS','4.6m overall PASS independent of block');
  s=activationStationCriterion({{threshold_m:350,threshold_status:'CONFIRMED',distance_m:280,block_share_pct:null}},{{}});
  assert(s.overall.status==='PASS','280m within confirmed 350m PASS');
  s=activationStationCriterion({{threshold_m:250,threshold_status:'CONFIRMED',distance_m:280,block_share_pct:60}},{{}});
  assert(s.direct.status==='FAIL' && s.overall.status==='FAIL','280m outside confirmed 250m FAIL');
  assert(s.block.status==='PASS','block is independently PASS');
  s=activationStationCriterion({{threshold_m:350,threshold_status:'CONFIRMED',distance_m:400,block_share_pct:60}},{{}});
  assert(s.overall.status==='FAIL','400m remains FAIL');
  assert(s.block.status==='PASS','400m case preserves independent block result');
  console.log('PASS dynamic route/station mocks');
}})().catch(e=>{{console.error(e.stack||e);process.exit(1)}});
'''
    with tempfile.NamedTemporaryFile('w',suffix='.js',delete=False,encoding='utf-8') as f:
        f.write(js); path=f.name
    subprocess.run(['node',path],check=True)

def main():
    # UI / data reuse
    check('collapsed inline layer UI','<details class="main-map-layer-details" id="mainMapLayerDetails">' in BASE and '<details class="main-map-layer-details" id="mainMapLayerDetails" open>' not in BASE)
    check('no floating leaflet layer control',"L.control({position:'topright'})" not in BASE)
    for ident in ['mainLayerSatelliteToggle','mainLayerZoningToggle','mainLayerCadastralToggle','mainLayerFacilityToggle']:
        check(f'layer toggle {ident}',BASE.count(f'id="{ident}"')==1)
    check('zoning default 92','id="mainLayerZoningOpacity" type="range" min="0" max="100" value="92"' in BASE)
    check('facility default 88','id="mainLayerFacilityOpacity" type="range" min="0" max="100" value="88"' in BASE)
    check('z-order',"['mainSatellitePane',200]" in BASE and "['mainZoningPreviewPane',320],['mainZoningFactPane',321]" in BASE and "['mainCadastralPreviewPane',330],['mainCadastralFactPane',331]" in BASE and "['mainFacilityPreviewPane',340],['mainFacilityFactPane',341]" in BASE)
    check('cadastral color switch',"mainMapLayerChecked('mainLayerZoningToggle'))return '#111827'" in BASE)
    check('pre-review zoning WMS',"layers:'tile_map:upis_c_uq111'" in BASE)
    check('pre-review cadastral WMS',"layers:'tile_map:s_lsmd_cbnd'" in BASE)
    check('pre-review facility WMS',"layers:'tile_map:upis_c_uq151,tile_map:upis_c_uq152,tile_map:upis_c_uq153'" in BASE)
    check('preview is display-only','판정 로직은 아래 WMS를 절대 참조하지 않으며' in BASE)
    check('preview toggle independent of fact count',"setMainMapLayerVisible(mainMapZoningPreview,zoneOn&&zoneOpacity>0)" in BASE and "setMainMapLayerVisible(mainMapFacilityPreview,facilityOn&&facilityOpacity>0)" in BASE)
    check('reuse parcel fact','const parcels=compactParcelBaseFeatures();' in BASE)
    check('reuse zoning fact','for(const row of planningAnalysis.zoning||[])' in BASE)
    check('reuse facility fact','for(const row of planningAnalysis.facilities||[])' in BASE)
    check('planning one-click preset','function setMainMapPlanningPreset()' in BASE)

    # 3-column road summary readability
    check('road summary compact columns', '#widthRoadSchemeSummary .station-scheme-row' in BASE and 'grid-template-columns:minmax(92px,118px) minmax(58px,72px) minmax(0,1fr)' in BASE)
    check('frontage summary compact columns', '#frontageSchemeSummary .station-scheme-row' in BASE and '#urbanRenewalFrontageSummary .station-scheme-row' in BASE)

    # street-block analysis is the first substantive review step and is not duplicated later
    review=extract_function('runSiteReview')
    auto=extract_function('runAllAutoAnalyses')
    check('street block runs before parcels', review.find("safeAnalysisStep('제도별 가로구역'") < review.find("safeAnalysisStep('연속지적'"))
    check('street block runs before station', review.find("safeAnalysisStep('제도별 가로구역'") < review.find("safeAnalysisStep('역세권 경계'"))
    check('street block duplicate skipped', 'skipStreetBlocks:true' in review and 'if(!options.skipStreetBlocks)' in auto)

    # REVIEW / FAIL separation
    src=extract_function('schemeRoadEvidenceFacts')
    check('growth null=>REVIEW',"status:(raw.maxWidth==null||raw.road35Perimeter==null)?'REVIEW'" in src)
    check('longterm null=>REVIEW',"status:(!longtermGeometryKnown||c.arterialIntersectionDist==null)?'REVIEW'" in src)
    check('innovation growth null=>REVIEW',"status:raw.maxWidth==null?'REVIEW'" in src)
    check('innovation housing null=>REVIEW',"status:(housingThreshold==null||raw.maxWidth==null||c.enclosed6==null)?'REVIEW'" in src)

    arterial=extract_function('analyzeActivationArterial')
    render=extract_function('renderActivationArterialSpatialStatus')
    check('arterial catch=>REVIEW',"routeStatus='REVIEW'" in arterial and '자동분석 오류 · 공개 GIS 자동판정 확인불가' in arterial)
    check('arterial renderer REVIEW label',"routeStatus==='REVIEW'?'확인필요'" in render)

    station=extract_function('activationStationCriterion')
    check('station overall direct-only','let status=directStatus' in station and 'shareKnown&&directStatus' not in station)
    check('station-complex block criterion preserved','function checkStationComplexFromFacts' in BASE and "schemeRow('사업대상지 가로구역 점유'" in BASE)

    dynamic_js_checks()
    print('ALL TARGETED CHECKS PASSED')

if __name__=='__main__': main()
