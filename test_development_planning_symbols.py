"""Offline verification of Appendix-1 national planning symbology.

The official 1:5,000 entries must be derived from the supplied source catalog,
not from feature order or arbitrary RGB palettes.
"""
import io
import unittest
import xml.etree.ElementTree as ET

import ezdxf
from shapely.geometry import box, mapping
from pyproj import Transformer
from shapely.ops import transform

from development_planning_symbols import catalog, style_for, hatch_pattern
from development_survey_sheet import zoning_context, planning_context, make_sheet, svg_sheet, dxf_paper_sheet

BACK = Transformer.from_crs('EPSG:5179', 'EPSG:4326', always_xy=True).transform


def geo(x1=953000, y1=1950000, x2=953120, y2=1950120):
    return mapping(transform(BACK, box(x1, y1, x2, y2)))


class PlanningSymbolsTest(unittest.TestCase):
    def test_catalog_complete_source_categories(self):
        types = catalog()['types']
        self.assertGreaterEqual(len(types), 60)
        self.assertEqual(len({(s['group'], s['name']) for s in types}), len(types))
        for group in ('zoning', 'district', 'area', 'facility'):
            self.assertTrue(any(s['group'] == group for s in types))
        self.assertEqual(catalog()['scale_denominator'], 5000)

    def test_aci_and_glyphs_from_original_guideline(self):
        cases = (
            ('제1종일반주거지역', 51, None),
            ('제2종일반주거지역', 2, None),
            ('제3종일반주거지역', 40, None),
            ('제2종전용주거지역', 2, 'circles'),
            ('준주거지역', 2, 'diagonal'),
            ('일반상업지역', 231, None),
            ('준공업지역', 191, 'diagonal'),
            ('자연녹지지역', 71, None),
            ('보전관리지역', 71, 'circles'),
            ('계획관리지역', None, 'diagonal'),
            ('도시지역', 1, None),
            ('자연환경보전지역', 131, 'circles'),
        )
        for name, aci, pat in cases:
            with self.subTest(name=name):
                data = style_for(name)
                self.assertTrue(data['verified_style'])
                self.assertEqual(data['aci'], aci)
                self.assertEqual(data['pattern'], pat)

    def test_unknown_not_automatically_confirmed(self):
        self.assertFalse(style_for('미분류 용도지역')['verified_style'])
        self.assertFalse(style_for('일반시설', 'facility')['verified_style'])
        self.assertFalse(style_for('지정구역', 'area')['verified_style'])

    def test_classes_do_not_depend_on_feature_order(self):
        inputs = [
            {'type':'Feature', 'properties':{'uname':'제2종일반주거지역'},
             'geometry':geo(953000,1950000,953060,1950120)},
            {'type':'Feature', 'properties':{'uname':'제1종일반주거지역'},
             'geometry':geo(953060,1950000,953120,1950120)},
        ]
        a=zoning_context(inputs)
        b=zoning_context(list(reversed(inputs)))
        index=lambda v:{x['name']:x['aci'] for x in v['legend']}
        self.assertEqual(index(a),index(b))
        self.assertEqual(index(a)['제1종일반주거지역'],51)
        self.assertEqual(index(a)['제2종일반주거지역'],2)

    def test_raster_preview_and_editable_paper_space(self):
        site=geo()
        plan=planning_context([
            {'type':'Feature','properties':{'name':'제2종전용주거지역'},
             'geometry':site}], 'zoning')
        parcels={'status':'COMPLETE','features':[{'type':'Feature','properties':{'jibun':'1-1'},
                                                  'geometry':site}]}
        item={'id':'5-2','name':'용도지역','features':plan['features'],'legend':plan['legend']}
        snapshot={'geometry':site,'_cad_background':parcels}
        ops=make_sheet(item,snapshot)
        self.assertTrue(any((op.get('symbol') or {}).get('pattern')=='circles' for op in ops))
        svg=svg_sheet(ops)
        ET.fromstring(svg)
        self.assertIn(b'<pattern',svg)
        doc=ezdxf.new('R2010')
        dxf_paper_sheet(doc,ops)
        self.assertTrue(any(e.dxftype()=='CIRCLE' for e in doc.layouts.get('SURVEY_A3')))
        buff=io.StringIO();doc.write(buff)
        self.assertIsNotNone(ezdxf.read(io.StringIO(buff.getvalue())))

    def test_modelspace_aci_and_pattern_at_5000(self):
        d=ezdxf.new('R2010');m=d.modelspace()
        symbol=style_for('준주거지역')
        n=hatch_pattern(m,box(0,0,200,200),'SURVEY_5_2',symbol,5.0)
        self.assertEqual(n,2)
        h=list(m.query('HATCH'))
        self.assertEqual(len(h),2)
        self.assertEqual([v.dxf.color for v in h],[2,1])
        self.assertTrue(h[1].pattern is not None)
        # On a 1:5000 model-space layout 3mm means 15 meters.
        lines=h[1].pattern.lines
        self.assertEqual(len(lines),1)
        self.assertAlmostEqual(abs(lines[0].offset[0]),15/(2**0.5),places=4)


    def test_management_dotted_boundary_and_edge_ticks(self):
        for name, group, expected in (
            ('지구단위계획구역', 'area', 'CIRCLE'),
            ('고도지구', 'district', 'LINE'),
        ):
            with self.subTest(name=name):
                doc=ezdxf.new('R2010')
                m=doc.modelspace()
                symbol=style_for(name,group)
                hatch_pattern(m,box(0,0,80,60),'SURVEY_PLAN',symbol,1.0)
                self.assertGreater(len(list(m.query(expected))),0)

    def test_planning_svg_and_dxf_generic_forms(self):
        site=geo()
        parcels={'status':'COMPLETE','features':[{'type':'Feature','properties':{'jibun':'1-1'},
                                                  'geometry':site}]}
        snap={'geometry':site,'_cad_background':parcels}
        items=(('5-3','district','고도지구'),('5-8','area','지구단위계획구역'),
               ('5-6','facility','근린공원'))
        for item_id,group,name in items:
            with self.subTest(name=name):
                classified=planning_context([{'type':'Feature','properties':{'name':name},
                                              'geometry':site}],group)
                item={'id':item_id,'name':name,'features':classified['features'],
                      'legend':classified['legend']}
                ops=make_sheet(item,snap)
                self.assertTrue(any((v.get('symbol') or {}).get('verified_style')
                                    for v in ops if v['type']=='polygon'))
                svg=svg_sheet(ops)
                ET.fromstring(svg)
                doc=ezdxf.new('R2010')
                dxf_paper_sheet(doc,ops)
                self.assertTrue(len(doc.layouts.get('SURVEY_A3'))>0)

    def test_school_subtype_and_unclassified_road_are_not_guessed(self):
        self.assertEqual(style_for('대학교','facility')['school_variant'],'university')
        self.assertEqual(style_for('중학교','facility')['school_variant'],'secondary')
        self.assertEqual(style_for('초등학교','facility')['school_variant'],'elementary')
        self.assertIsNone(style_for('학교','facility')['school_variant'])
        self.assertIsNone(style_for('도로','facility')['line_color'])
        self.assertEqual(style_for('대로','facility')['line_color'],'red')
        self.assertEqual(style_for('중로','facility')['line_color'],'blue')

    def test_school_black_glyph_editability(self):
        site=geo()
        categorized=planning_context([{'type':'Feature',
                                       'properties':{'name':'초등학교'},
                                       'geometry':site}], 'facility')
        ops=make_sheet({'id':'5-7','name':'기타 시설',
                        'features':categorized['features'],
                        'legend':categorized['legend']},
                       {'geometry':site,'_cad_background':{'status':'COMPLETE','features':[]}})
        self.assertTrue(any(op['type']=='school_mark' for op in ops))
        svg=svg_sheet(ops)
        ET.fromstring(svg)
        doc=ezdxf.new('R2010')
        dxf_paper_sheet(doc,ops)
        self.assertTrue(len(list(doc.layouts.get('SURVEY_A3').query('HATCH')))>0)

    def test_linear_pipe_plan_symbol_in_paper_dxf(self):
        from shapely.geometry import LineString
        line=LineString([(953005,1950005),(953115,1950115)])
        shaped=mapping(transform(BACK,line))
        site=geo()
        classified=planning_context([{'type':'Feature','properties':{'name':'가스공급설비'},
                                       'geometry':shaped}], 'facility')
        item={'id':'5-7','name':'기타 도시계획시설',
              'features':classified['features'],'legend':classified['legend']}
        snap={'geometry':site,'_cad_background':{'status':'COMPLETE','features':[]}}
        ops=make_sheet(item,snap)
        self.assertTrue(any(v['type']=='polyline' and (v.get('symbol') or {}).get('pattern')=='line_dots'
                            for v in ops))
        doc=ezdxf.new('R2010')
        dxf_paper_sheet(doc,ops)
        self.assertTrue(len(list(doc.layouts.get('SURVEY_A3').query('CIRCLE'))) > 0)


if __name__ == '__main__':
    unittest.main()
