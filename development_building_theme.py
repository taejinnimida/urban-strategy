"""Building-footprint survey themes. No redevelopment eligibility or inferred legal age."""
import hashlib
import re
from collections import defaultdict
from shapely.geometry import shape, mapping

FIELDS = {'3-1':'use','3-2':'structure','3-3':'floors','3-4':'age',
          '3-5':'floor_area','3-6':'coverage','3-7':'far'}
PALETTE = ('#4477aa','#ee8866','#228833','#aa3377','#66ccee',
           '#ccbb44','#8844aa','#44aa99','#bb6677','#999933')


def _pnu(props):
    for key in ('pnu','PNU'):
        val = str(props.get(key) or '').strip()
        if re.fullmatch(r'\d{19}',val):
            return val
    return None


def attach_building_themes(data, snapshot):
    """Preserve raw spatial outlines and mark uncertain ledger joins as unknown.

    A one-to-one ledger/footprint intersection inside one cadastral parcel is a
    tentative *spatial* link, not proof of building-register identity.
    """
    layer = snapshot.get('_report_layers',{}).get('LT_C_SPBD') or {}
    fetched = layer.get('status') in ('COMPLETE','REFERENCE')
    verified = layer.get('status') == 'COMPLETE'
    site = shape(snapshot['geometry'])
    parcel_layer = snapshot.get('_report_layers',{}).get('LP_PA_CBND_BUBUN') or {}
    parcel_geoms = defaultdict(list)
    for parcel in parcel_layer.get('features',[]):
        props = parcel.get('properties') or {}
        pnu = _pnu(props)
        if not pnu:
            pnu = str(parcel.get('id') or '')
        if re.fullmatch(r'\d{19}',pnu):
            try:
                parcel_geoms[pnu].append(shape(parcel['geometry']))
            except (ValueError,KeyError,TypeError):
                pass

    by_pnu=defaultdict(list)
    for row in data['buildings']:
        for pnu in str(row.get('pnus') or '').split(','):
            pnu=pnu.strip()
            if re.fullmatch(r'\d{19}',pnu):
                by_pnu[pnu].append(row)

    footprints=[]
    for feature in layer.get('features',[]) if fetched else []:
        try:
            geom=shape(feature['geometry'])
            if geom.is_empty or not geom.is_valid or geom.geom_type not in ('Polygon','MultiPolygon'):
                continue
            clipped=geom.intersection(site)
            if clipped.is_empty or clipped.area <= 0:
                continue
            props=feature.get('properties') or {}
            explicit=_pnu(props)
            candidates=[]
            if explicit in by_pnu:
                candidates=[explicit]
            else:
                for pnu, geoms in parcel_geoms.items():
                    if pnu not in by_pnu:
                        continue
                    total=sum(clipped.intersection(pg).area for pg in geoms)
                    if total / clipped.area > .95:
                        candidates.append(pnu)
            footprints.append({'geometry':mapping(clipped),'id':str(feature.get('id') or ''),
                              'candidates':sorted(set(candidates)),'explicit':explicit,
                              'source_properties':props})
        except (ValueError,TypeError,KeyError):
            continue

    # Direct management-number equality is the only confirmed thematic join.
    # PNU/intersection is retained for diagnostics only, never promoted to
    # a confirmed use / structure / storey / age / density category.
    ledger_by_id = {str(row['id']).strip(): row for row in data['buildings']
                    if row.get('id')}
    direct_ids = ('mgmBldrgstPk', 'mgmbldrgstpk', 'mgm_bldrgst_pk')
    linked = 0
    provisionally_located = 0
    for foot in footprints:
        foot['ledger'] = None
        foot['join'] = 'UNMATCHED'
        raw = foot.get('source_properties') or {}
        key = next((str(raw.get(name) or '').strip() for name in direct_ids
                    if raw.get(name) not in (None, '')), '')
        if key and key in ledger_by_id:
            foot['ledger'] = ledger_by_id[key]
            foot['join'] = 'EXACT_ID'
            linked += 1
        elif foot['candidates']:
            provisionally_located += 1

    for item in data['items']:
        field=FIELDS.get(item['id'])
        if field is None:
            continue
        if not fetched:
            item.update(features=[],provider_verified=False,
                        detail=(item.get('detail') or '')+' · 건물 외곽선 공간도형 미확보',
                        geometry_status='UNAVAILABLE')
            continue
        categories={}
        features=[]
        for f in footprints:
            value=f['ledger'].get(field) if f['ledger'] else None
            key=str(value) if value is not None and value!='' else '대장 속성 미확인'
            if key not in categories:
                color='#a1a1aa' if key=='대장 속성 미확인' else PALETTE[int(hashlib.sha256(key.encode('utf-8')).hexdigest()[:8],16)%len(PALETTE)]
                categories[key]={'name':key,'color':color,'count':0,
                                 'class_code':'C99' if key=='대장 속성 미확인' else 'C'+str(len(categories)+1).zfill(2)}
            categories[key]['count']+=1
            features.append({'type':'Feature','geometry':f['geometry'],
                             'properties':{'name':key,'color':categories[key]['color'],
                                           'class_code':categories[key]['class_code'],
                                           'join_status':f['join'],
                                           'building_id':(f['ledger'] or {}).get('id')}})
        item.update(features=features,legend=list(categories.values()),thematic_type='building',
                    provider_verified=verified,geometry_status='COMPLETE' if verified else 'REFERENCE',
                    status='공간자료 확보' if verified else '보조 조회 참고자료',
                    detail=(item.get('detail') or '')+
                        f' · 건축물 도형 {len(footprints)}동, 대장 관리번호 일치 {linked}동, 속성 미확인 {len(footprints)-linked}동'
                        +f' · 공간상 대장 관련 가능 {provisionally_located}동(미확인에 포함)'
                        +' · 미확인을 무허가 건축물로 단정하지 않음')
        # Existing register-based distributions are not overwritten.
        stat=data.get('statistics',{}).get(item['id'])
        if isinstance(stat,dict):
            stat['footprint_count']=len(footprints)
            stat['verified_ledger_joins']=linked
            stat['possible_spatial_joins']=provisionally_located
            stat['unmatched_footprints']=len(footprints)-linked
            stat['footprint_status']=item['geometry_status']
            stat['note']=(stat.get('note') or '')+' 도형 전체를 표시하며 관리번호가 확인되지 않은 도형은 속성 미확인으로 집계함. 무허가 여부는 판정하지 않음.'
