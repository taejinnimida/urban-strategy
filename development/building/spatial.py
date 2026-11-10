"""Official GIS-building geometry themes for survey 3-1 / 3-2.

A Building HUB title record is NOT a mapped building footprint. Spatial tables,
SVG and CAD consume only the separately registered, provenance-checked GIS
building geometry. Do not join by parcel alone or reuse parcel surface as a
building polygon. The HUB ledger table is retained separately for comparison.
"""
from collections import defaultdict
import hashlib
import json

from shapely.geometry import mapping, shape
from shapely.ops import unary_union
from pyproj import Transformer
from shapely.ops import transform


COLORS=('#c0dcf2','#86bbda','#5284b8','#c5dba8','#83b98c','#e4c37b','#e3a77f','#c9a3d1','#a4aab7')
FIELDS={'3-1':('use','용도'),'3-2':('structure','구조')}
METRIC=Transformer.from_crs('EPSG:4326','EPSG:5179',always_xy=True).transform


def _name(v):
    if not isinstance(v,str):return None
    v=v.strip()
    return v[:120] if v and v not in ('분류 미확보','null','None') else None


def attach_building_spatial_themes(data,snapshot,area):
    """Two faithful GIS footprint maps, only if real official spatial data exist."""
    indexes={v.get('id'):v for v in data.get('items') or []}
    # Exact management-key join is allowed. PNU-only or spatial-nearest
    # matching can assign a building's use/age to the wrong footprint.
    hub={str(v['id']):v for v in data.get('buildings',[])
         if isinstance(v,dict) and v.get('id')}
    sources=snapshot.get('_national_sources') or {}
    live=sources.get('building_gis') or {}
    registered=sources.get('file:3-spatial') or {}
    # Prefer direct provider geometry when it contains at least one feature;
    # verified original-file input remains a provenance-checkable fallback.
    # A failed live call never means zero buildings or replaces a good file.
    source=(live if live.get('status') in ('DATA','PARTIAL') and live.get('features')
            else registered if registered.get('status') in ('DATA','PARTIAL') and registered.get('features')
            else live if live else registered)
    for itemid,(field,word) in FIELDS.items():
        item=indexes.get(itemid)
        if item is None:continue
        item['spatial_status']='MISSING'
        item['spatial_detail']='공식 GIS건물통합정보 원도형이 등록·확인되지 않아 건물별 도면을 생성하지 않았습니다.'
        item['building_registry_table']=item.get('survey_table')
        if source.get('status') not in ('DATA','PARTIAL') or not source.get('features'):
            continue
        groups=defaultdict(list)
        details=[]
        seen=set()
        missing=0
        exact_hub_matches=set()
        try:
            for i,f in enumerate(source['features']):
                props=f.get('properties') or {}
                g=shape(f['geometry'])
                if g.is_empty or not g.is_valid or g.geom_type not in ('Polygon','MultiPolygon'):
                    raise ValueError('invalid building footprint')
                matching_hub=hub.get(str(props.get('hub_management_id') or ''))
                if matching_hub:
                    exact_hub_matches.add(str(props['hub_management_id']))
                category=_name(props.get(field))
                if category is None and matching_hub:
                    category=_name(matching_hub.get(field))
                if category is None:
                    missing+=1
                    category='분류 미확보'
                # Duplicate geometry+class is never counted as an additional
                # building. A multi-part feature is one spatial source record.
                stable_id=str(props.get('building_id') or '').strip()
                key=stable_id if stable_id else hashlib.sha256(
                    json.dumps(mapping(g),sort_keys=True).encode()).hexdigest()
                if key in seen:
                    raise ValueError('duplicate building source ID/geometry')
                seen.add(key)
                groups[category].append(g)
                details.append((g,category,key))
            if not details:
                continue
            site=shape(snapshot['geometry'])
            site_area=area(site)
            if site_area<=0:
                raise ValueError('invalid boundary')
            legend=[];code={}
            for index,name in enumerate(sorted(groups)):
                col=('#d5d9de' if name=='분류 미확보' else COLORS[index%len(COLORS)])
                code[name]=(f'C{index+1:02d}',col)
                combined=unary_union(groups[name])
                legend.append({'name':name,'class_code':code[name][0],'color':col,
                               'area_m2':round(area(combined),2),
                               'count':len(groups[name])})
            union=unary_union([g for g,_,_ in details])
            covered=round(area(union),2)
            if covered<=0:raise ValueError('empty building geometry')
            sums=round(sum(v['area_m2'] for v in legend),2)
            overlap=max(0,round(sums-covered,2))
            features=[]
            for g,name,ident in details:
                cls,color=code[name]
                features.append({'type':'Feature','geometry':mapping(g),
                                 'properties':{'name':name,'class_code':cls,'color':color,
                                               'building_id':ident}})
            rows=[{'kind':'total','label':'GIS 건물도형 합집합','count':len(details),
                   'area_m2':covered,'pct':100.0,
                   'note':'원도형 대상지 편입분'}]
            for group in legend:
                rows.append({'kind':'category','label':group['name'],'count':group['count'],
                             'area_m2':group['area_m2'],
                             'pct':round(100*group['area_m2']/covered,1),
                             'note':'공식 GIS 도형 기준' if group['name']!='분류 미확보' else '분류 미확보'})
            if overlap>max(.05,site_area*.00001):
                rows.append({'kind':'warning','label':'분류별 면적 중첩',
                             'count':None,'area_m2':overlap,
                             'pct':round(100*overlap/covered,1),
                             'note':'합집합과 분류면적 합의 차이'})
            note=['건축물대장 표제부 건수와 GIS건물통합정보 도형수는 다른 모집단입니다.',
                  '구성비의 분모는 GIS 건물도형 합집합 면적이며 구역계 전체면적이 아닙니다.',
                  'GIS 도형별 용도·구조 분류에 근거하며, 편입필지 면적을 건축물 면적으로 사용하지 않습니다.',
                  f'건축HUB 관리번호 일치: {len(exact_hub_matches)}/{len(details)} GIS 도형. 일치하지 않는 건물의 대장 속성은 추정 결합하지 않습니다.',
                  '원본 GIS 도형의 수록 완전성과 건축물대장 관리번호 일치 여부는 별도 확인 대상입니다.']
            if missing:note.append(f'GIS 도형 분류 미확보 {missing}건. 법정 적격성으로 해석하지 않습니다.')
            description=(f'공식 GIS건물통합정보의 구역계 교차 도형 {len(details)}건을 '
                         f'{word}별로 구분하였다. 도형 합집합 면적은 {covered:,.2f}㎡이다.'
                         ' 본 집계는 건축HUB 표제부 건수와 별도이며 직접 합산하지 않는다.')
            table={'item_id':itemid,'title':f'표 {itemid} GIS건물통합정보 {word}별 공간현황',
                   'status':'COMPLETE' if not missing and overlap<=max(.05,site_area*.00001) else 'PARTIAL',
                   'count_header':'도형수(개)','source':source.get('source') or 'GIS건물통합정보',
                   'count_basis':'공식 GIS 원도형, 구역계 편입도형',
                   'rows':rows,'notes':note,'analysis_text':description,
                   'total_count':len(details),'known_count':len(details)-missing,
                   'missing_count':missing}
            item.update(status='공간자료 확보' if table['status']=='COMPLETE' else '일부 확보',
                        detail=f'GIS건물통합정보 {len(details)}개 도형 · {word}별 공간분류 · 건축HUB 표제부와 독립된 집계',
                        source=table['source'],features=features,
                        legend=legend,thematic_type='building_spatial',
                        # A category can be complete even if the *other*
                        # category is missing (e.g., use present, structure absent).
                        provider_verified=table['status']=='COMPLETE',
                        spatial_status=table['status'],spatial_detail=description,
                        survey_table=table)
            data.setdefault('statistics',{})[itemid]={
                'groups':legend,'building_feature_count':len(details),
                'mapped_footprint_area_m2':covered,
                'site_area_m2':round(site_area,2),
                'source':'GIS건물통합정보 편입 도형',
                'hub_exact_match_count':len(exact_hub_matches),
                'hub_total_title_count':len(hub)}
            item['statistics']=data['statistics'][itemid]
            item['source_coverage_assurance']=source.get('coverage_assurance','REGISTERED_OFFICIAL_FILE')
            item['source_acquisition']=source.get('acquisition','REGISTERED_FILE')
            item['registry_footprint_join_status']='NOT_VERIFIED'
        except (KeyError,ValueError,TypeError,OverflowError):
            item['spatial_status']='ERROR'
            item['spatial_detail']='GIS건물통합정보 도형·분류 검증 오류로 도면을 출력하지 않습니다.'
            item.pop('features',None)
            item.pop('legend',None)
            item['provider_verified']=False
