from pathlib import Path
import re, sys

html = Path('app.html').read_text(encoding='utf-8')
checks = []
def check(label, cond):
    checks.append((label, bool(cond)))
    print(('PASS' if cond else 'FAIL'), label)

check('parcel tab renamed', '>지번으로 찾기</button>' in html)
check('parcel input subtab', 'id="boundaryParcelInputTab"' in html and '>입력하기</button>' in html)
check('parcel click subtab', 'id="boundaryParcelClickTab"' in html and '>클릭하기</button>' in html)
check('shared selection summary', 'id="boundaryParcelSelectionSummary"' in html and '합계면적' in html)
check('shared boundary confirm button', 'id="applyParcelFindBoundaryBtn"' in html and 'applySelectedParcelsAsBoundary()' in html)
check('click mode state', "let parcelFindMode='input'" in html and 'function setParcelFindMode(mode)' in html)
check('map click lookup handler', "map.on('click',handleParcelFindMapClick)" in html and 'async function handleParcelFindMapClick(e)' in html)
check('point parcel lookup', 'async function fetchParcelAtMapPoint(latlng)' in html and 'turf.booleanPointInPolygon' in html)
check('click result joins selectedParcelPnus', re.search(r'handleParcelFindMapClick[\s\S]{0,3000}selectedParcelPnus\.add\(String\(key\)\)', html) is not None)
check('address result joins same selection set', re.search(r'lookupBoundaryAddresses[\s\S]{0,3500}selectedParcelPnus\.add\(String\(key\)\)', html) is not None)
check('parcel feature click stops double propagation', 'L.DomEvent.stopPropagation(e)' in html)
check('address preview noninteractive', 'const addressPreviewLayer=L.geoJSON(null,{\n  interactive:false' in html)
check('selection area metric', 'function selectedParcelUiMetrics()' in html and 'geometry_area_m2' in html)
check('selected parcel fit action', 'function fitToSelectedParcels()' in html)
check('disconnected parcel warning', '분리된 구역으로 구성됩니다' in html and 'window.confirm' in html)
check('existing R21 frontage retained', '4m 기준 접도율' in html and '6m 기준 접도율' in html)
check('existing R20 area prefilter retained', all(x in html for x in ['analysis_area_cut_m2:24000','analysis_area_cut_m2:36000','analysis_area_cut_m2:12000']))
py=Path('app.py').read_text(encoding='utf-8')
check('existing R23 graph retained', '_NeighborTopology' in py or 'neighbor_topology' in py or 'neighbor_cache' in py)

failed=[x for x in checks if not x[1]]
if failed:
    print(f'FAILED {len(failed)}/{len(checks)}')
    sys.exit(1)
print(f'ALL R24 TARGETED CHECKS PASS ({len(checks)}/{len(checks)})')
