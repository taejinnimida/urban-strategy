"""Descriptive, non-statutory DEM elevation/slope groups; projected metre areas only."""
from collections import defaultdict
import math

ELEVATION_EDGES=(0.,20.,40.,60.,80.)
SLOPE_EDGES=(5.,10.,15.,20.)
ELEVATION_COLORS=('#334155','#dbeafe','#a6c7e8','#71a2d2','#3877b5','#174c86')
SLOPE_COLORS=('#d8f0d0','#a4d99d','#f5e48e','#f2ad69','#d85d53')


def classify(value,kind):
    """Classes are left-inclusive and right-exclusive, except the last."""
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):
        return None
    if kind=='elevation':
        edges,colors=ELEVATION_EDGES,ELEVATION_COLORS
        labels=('0m 미만','0~20m','20~40m','40~60m','60~80m','80m 이상')
    elif kind=='slope':
        if value<0:return None
        edges,colors=SLOPE_EDGES,SLOPE_COLORS
        labels=('0~5°','5~10°','10~15°','15~20°','20° 이상')
    else:
        raise ValueError('unknown terrain theme')
    index=next((i for i,edge in enumerate(edges) if value<edge),len(edges))
    return f'C{index+1:02}',labels[index],colors[index]


def new_accumulator(kind):
    return {'kind':kind,'classes':defaultdict(lambda:{'area_m2':0.,'count':0}),'measured_area_m2':0.}


def add_cell(acc,value,area_m2):
    cls=classify(value,acc['kind'])
    if cls is None:return None
    if (isinstance(area_m2,bool) or not isinstance(area_m2,(int,float)) or
            not math.isfinite(area_m2) or area_m2<=0):
        raise ValueError('invalid positive projected cell area')
    entry=acc['classes'][cls]
    entry['area_m2']+=area_m2
    entry['count']+=1
    acc['measured_area_m2']+=area_m2
    return cls


def finish(acc,total_site_area_m2):
    if (isinstance(total_site_area_m2,bool) or not isinstance(total_site_area_m2,(int,float)) or
            not math.isfinite(total_site_area_m2) or total_site_area_m2<=0):
        raise ValueError('invalid site area')
    groups=[]
    for (code,name,color),values in sorted(acc['classes'].items()):
        groups.append({'class_code':code,'name':name,'color':color,
                       'count':values['count'],'area_m2':round(values['area_m2'],2),
                       'pct':round(values['area_m2']*100/total_site_area_m2,1)})
    valid=acc['measured_area_m2']
    return {'groups':groups,'valid_area_m2':round(valid,2),
            'coverage_pct':round(valid*100/total_site_area_m2,3),
            'missing_area_m2':round(max(0.,total_site_area_m2-valid),2),
            'area_method':'projected metric DEM cell/site geometry intersections',
            'classification_rule':'left inclusive / right exclusive, last inclusive'}
