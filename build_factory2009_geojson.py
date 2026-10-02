import json, time
from collections import defaultdict
from pathlib import Path
from shapely.geometry import Polygon, MultiPolygon, GeometryCollection, mapping
from shapely.ops import unary_union, transform
from shapely.validation import make_valid
from pyproj import Transformer

DXF=Path('/mnt/data/2009_준공업지역_공장비율_6개구_작업용_완료.dxf')
OUT=Path('/mnt/data/r57work/semiindustrial_factory_ratio_2009.geojson')
OUT_AUDIT=Path('/mnt/data/r57work/semiindustrial_factory_ratio_2009_audit.json')
ENC='cp949'
LAYER_MAP={
    '10_미만':('LT10','10% 미만',0,10),
    '10_30_':('10_30','10~30%',10,30),
    '30_50_':('30_50','30~50%',30,50),
    '50_이상':('GE50','50% 이상',50,100),
    '도시계획시설':('FACILITY','도시계획시설',None,None),
}
SEOUL_BOUNDS=(170000,530000,215000,575000)  # finished CAD / EPSG:5186 sanity envelope


def _poly_only(g):
    if g is None or g.is_empty:return None
    if isinstance(g,(Polygon,MultiPolygon)):return g
    if isinstance(g,GeometryCollection):
        ps=[]
        for x in g.geoms:
            y=_poly_only(x)
            if y is not None and not y.is_empty:ps.append(y)
        return unary_union(ps) if ps else None
    return None


def _plausible_coords(coords):
    xmin,ymin,xmax,ymax=SEOUL_BOUNDS
    return bool(coords) and all(xmin <= x <= xmax and ymin <= y <= ymax for x,y in coords)


def _valid_polygon(coords):
    if len(coords)<3 or not _plausible_coords(coords):return None
    # remove immediate duplicate points; close is implicit in Polygon
    clean=[]
    for p in coords:
        if not clean or p!=clean[-1]:clean.append(p)
    if len(clean)>2 and clean[0]==clean[-1]:clean.pop()
    if len(clean)<3:return None
    try:g=Polygon(clean)
    except Exception:return None
    if g.is_empty or abs(g.area)<0.01:return None
    if not g.is_valid:
        try:g=_poly_only(make_valid(g))
        except Exception:g=None
    if g is None or g.is_empty or g.area<0.01:return None
    return g


def _xdata_polygon_candidates(ent):
    """Recover R14 hatch boundary from two encodings used by R12 export.

    A) polyline loop: 1071 n followed by 2*n consecutive 1040 x/y values.
    B) edge loop:     1071 n followed by n repetitions of 1070 1 + 4 x 1040
                      (line edge x1,y1,x2,y2).
    """
    out=[]
    for i,(c,v) in enumerate(ent):
        if c!=1071:continue
        try:n=int(v.strip())
        except Exception:continue
        if n<3 or n>10000:continue

        # A. contiguous vertex list
        vals=[];j=i+1
        while j<len(ent) and ent[j][0]==1040 and len(vals)<2*n:
            try:vals.append(float(ent[j][1].strip()))
            except Exception:break
            j+=1
        if len(vals)==2*n:
            g=_valid_polygon([(vals[k],vals[k+1]) for k in range(0,len(vals),2)])
            if g is not None:out.append(('xdata_vertices',g))

        # B. line-edge loop
        j=i+1;edges=[]
        # tolerate non-edge metadata before first edge marker, but stop if another 1071 begins
        while j<len(ent) and len(edges)<n:
            c2,v2=ent[j]
            if c2==1071:break
            if c2!=1070:
                j+=1;continue
            try:edge_type=int(v2.strip())
            except Exception:break
            if edge_type!=1:break  # this source uses only line edges in the exceptional loops
            j+=1; q=[]
            while j<len(ent) and ent[j][0]==1040 and len(q)<4:
                try:q.append(float(ent[j][1].strip()))
                except Exception:break
                j+=1
            if len(q)!=4:break
            edges.append(((q[0],q[1]),(q[2],q[3])))
        if len(edges)==n:
            # Follow edge starts; append final endpoint if necessary.
            coords=[edges[0][0]]
            cur=edges[0][1]; coords.append(cur)
            for a,b in edges[1:]:
                # exported edges are sequential; pick orientation closest to current for robustness
                da=(a[0]-cur[0])**2+(a[1]-cur[1])**2
                db=(b[0]-cur[0])**2+(b[1]-cur[1])**2
                nxt=b if da<=db else a
                coords.append(nxt); cur=nxt
            g=_valid_polygon(coords)
            if g is not None:out.append(('xdata_edges',g))
    return out


def _scan_entities(path):
    """Read relevant INSERTs without loading the 255 MB DXF into memory."""
    inserts=[]; in_ent=False; typ=None; ent=[]
    def consume(t,e):
        if t!='INSERT' or not e:return
        layer=next((v.decode(ENC,'replace') for c,v in e if c==8),'')
        if layer not in LAYER_MAP:return
        block=next((v.decode(ENC,'replace') for c,v in e if c==2),'')
        handle=next((v.decode('ascii','ignore') for c,v in e if c==5),'')
        cands=_xdata_polygon_candidates(e)
        # If several XDATA candidates exist, choose the largest valid polygon area. This avoids
        # selecting R14 metadata values that happen to look like a vertex list.
        if cands:
            method,g=max(cands,key=lambda z:z[1].area)
            inserts.append({'layer':layer,'block':block,'handle':handle,'method':method,'geometry':g})
        else:
            inserts.append({'layer':layer,'block':block,'handle':handle,'method':'block_fallback','geometry':None})

    with open(path,'rb') as f:
        while True:
            c=f.readline();v=f.readline()
            if not v:break
            try:code=int(c.strip() or 0)
            except Exception:continue
            val=v.rstrip(b'\r\n')
            if code==2 and val==b'ENTITIES':in_ent=True;continue
            if not in_ent:continue
            if code==0:
                t=val.decode('ascii','ignore')
                if typ is not None:consume(typ,ent)
                if t=='ENDSEC':break
                typ=t;ent=[]
            else:ent.append((code,val))
    return inserts


def _solid_polygon(ent):
    vals={}
    for c,v in ent:
        if c in (10,20,11,21,12,22,13,23):
            try:vals[c]=float(v.strip())
            except Exception:pass
    if not all(k in vals for k in (10,20,11,21,12,22)):return None
    p0=(vals[10],vals[20]);p1=(vals[11],vals[21]);p2=(vals[12],vals[22]);p3=(vals.get(13,vals[12]),vals.get(23,vals[22]))
    # DXF SOLID/TRACE point ordering stores the 3rd/4th corners reversed for quads.
    coords=[p0,p1,p3,p2]
    # Triangles repeat the third point; valid_polygon removes immediate duplicates.
    return _valid_polygon(coords)


def _scan_block_solid_geometries(path,target_blocks):
    """Anonymous R12 hatch blocks lacking R14 boundary XDATA are triangulated as SOLID entities.
    Union their SOLIDs to recover the authoritative hatch footprint.
    """
    if not target_blocks:return {},{}
    geoms=defaultdict(list); solid_counts=defaultdict(int)
    in_blocks=False; current_block=None; typ=None; ent=[]

    def consume(t,e,block):
        if block not in target_blocks or t!='SOLID':return
        g=_solid_polygon(e)
        if g is not None:
            geoms[block].append(g); solid_counts[block]+=1

    with open(path,'rb') as f:
        while True:
            c=f.readline();v=f.readline()
            if not v:break
            try:code=int(c.strip() or 0)
            except Exception:continue
            val=v.rstrip(b'\r\n')
            if code==2 and val==b'BLOCKS':in_blocks=True;continue
            if not in_blocks:continue
            if code==0:
                t=val.decode('ascii','ignore')
                if typ is not None:consume(typ,ent,current_block)
                if t=='ENDSEC':break
                if t=='BLOCK':
                    current_block='__PENDING__';typ='BLOCK';ent=[]
                elif t=='ENDBLK':
                    current_block=None;typ='ENDBLK';ent=[]
                else:
                    typ=t;ent=[]
            else:
                ent.append((code,val))
                if typ=='BLOCK' and code==2:
                    current_block=val.decode(ENC,'replace')

    dissolved={}
    for b,ps in geoms.items():
        if not ps:continue
        # Anonymous hatch blocks can contain hundreds/thousands of SOLID triangles.
        chunks=[unary_union(ps[i:i+500]) for i in range(0,len(ps),500)]
        g=_poly_only(unary_union(chunks))
        if g is not None and not g.is_valid:
            g=_poly_only(make_valid(g))
        if g is not None and not g.is_empty and g.area>=0.01:dissolved[b]=g
    return dissolved,solid_counts


def extract_insert_polygons(path):
    inserts=_scan_entities(path)
    target_blocks={x['block'] for x in inserts if x['geometry'] is None and x['block']}
    block_geoms,block_solid_counts=_scan_block_solid_geometries(path,target_blocks)

    groups=defaultdict(list)
    stats=defaultdict(lambda:{'entities':0,'valid':0,'xdata_vertices':0,'xdata_edges':0,'block_fallback':0,
                              'block_missing':0,'invalid_or_implausible':0,'raw_area_m2':0.0})
    missing=[]
    for x in inserts:
        layer=x['layer'];st=stats[layer];st['entities']+=1
        g=x['geometry'];method=x['method']
        if g is None:
            g=block_geoms.get(x['block'])
            if g is not None:method='block_fallback'
        if g is None or g.is_empty or g.area<0.01:
            st['block_missing']+=1
            if len(missing)<100:missing.append({'layer':layer,'handle':x['handle'],'block':x['block']})
            continue
        # final sanity after union fallback
        minx,miny,maxx,maxy=g.bounds
        xmin,ymin,xmax,ymax=SEOUL_BOUNDS
        if minx<xmin or maxx>xmax or miny<ymin or maxy>ymax:
            st['invalid_or_implausible']+=1
            if len(missing)<100:missing.append({'layer':layer,'handle':x['handle'],'block':x['block'],'reason':'implausible_bounds','bounds':g.bounds})
            continue
        groups[layer].append(g);st['valid']+=1;st[method]+=1;st['raw_area_m2']+=float(g.area)

    block_meta={'target_blocks':len(target_blocks),'resolved_blocks':len(block_geoms),
                'solid_entities':int(sum(block_solid_counts.values())),'missing_examples':missing}
    return groups,stats,block_meta


start=time.time()
groups,stats,block_meta=extract_insert_polygons(DXF)
print('extract seconds',round(time.time()-start,2),{k:len(v) for k,v in groups.items()},block_meta)

# Dissolve each categorical layer in the authoritative CAD coordinate system (EPSG:5186).
dissolved={}
for layer,geoms in groups.items():
    t=time.time()
    chunks=[unary_union(geoms[i:i+500]) for i in range(0,len(geoms),500)]
    u=_poly_only(unary_union(chunks))
    if u is not None and not u.is_valid:u=_poly_only(make_valid(u))
    dissolved[layer]=u
    print('union',layer,'sec',round(time.time()-t,2),'geom',getattr(u,'geom_type',None),'area',round(u.area if u else 0,2))

# Cross-class overlap should be effectively zero. Report rather than silently clipping the authoritative source.
ratio_layers=['10_미만','10_30_','30_50_','50_이상']
overlap_checks={}
for i,a in enumerate(ratio_layers):
    ga=dissolved.get(a)
    if ga is None:continue
    for b in ratio_layers[i+1:]:
        gb=dissolved.get(b)
        if gb is None:continue
        try:ar=float(ga.intersection(gb).area)
        except Exception:ar=-1
        overlap_checks[f'{a}__{b}']=ar

# GeoJSON uses WGS84. No geometry simplification: user-corrected CAD is authoritative.
to_wgs=Transformer.from_crs(5186,4326,always_xy=True).transform
features=[]
for layer,(code,label,rmin,rmax) in LAYER_MAP.items():
    g=dissolved.get(layer)
    if g is None or g.is_empty:continue
    gw=transform(to_wgs,g)
    props={
        'factory_class':code,'class_label':label,'ratio_min':rmin,'ratio_max':rmax,
        'ratio_max_exclusive':bool(code in {'LT10','10_30','30_50'}),'source_layer':layer,
        'reference_date':'2008-01-31','source_title':'2009 서울시 준공업지역 종합발전계획 수립 용역 최종성과품',
        'source_basis':'사용자 보정 완료 DXF · 자치구별 가구별 공장비율 현황도',
        'quality':'USER_CORRECTED_CAD_REFERENCE','source_entity_count':stats[layer]['valid'],
        'area_m2_epsg5186':round(float(g.area),3),
    }
    features.append({'type':'Feature','properties':props,'geometry':mapping(gw)})

fc={
    'type':'FeatureCollection','name':'semiindustrial_factory_ratio_2009',
    'properties':{
        'source_crs':'EPSG:5186','geojson_crs':'EPSG:4326','reference_date':'2008-01-31',
        'source_title':'2009 서울시 준공업지역 종합발전계획 수립 용역 최종성과품','source_cad':DXF.name,
        'classes':['10% 미만','10~30%','30~50%','50% 이상','도시계획시설'],
        'districts':['영등포구','강서구','구로구','금천구','성동구','도봉구'],
        'note':'공장비율 현황도에 따라 사용자가 보정 완료한 CAD 해치를 공간 FACT로 변환. 법정계획 수립 단계에서는 공부·현장조사로 재확인.'
    },
    'features':features
}
OUT.write_text(json.dumps(fc,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
audit={'dxf':str(DXF),'stats':{k:dict(v) for k,v in stats.items()},'block_fallback':block_meta,
       'dissolved_area_m2':{k:round(float(v.area),3) for k,v in dissolved.items() if v is not None},
       'cross_class_overlap_m2':overlap_checks,'geojson_bytes':OUT.stat().st_size,'elapsed_sec':round(time.time()-start,2)}
OUT_AUDIT.write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(audit,ensure_ascii=False,indent=2))
print('geojson',OUT,OUT.stat().st_size,'bytes','total_sec',round(time.time()-start,2))
