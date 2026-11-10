"""Full CAD acceptance: editable frame, legend, double-dot site boundary.

No false acceptance based on a cropped map alone. Verify both the model
companion drawing and the print deliverable SURVEY_A3 after write/read.
"""
import io
import unittest

import ezdxf
from shapely.geometry import box, mapping
from shapely.ops import transform
from pyproj import Transformer

from development.cad import dxf_bytes
from development.survey_sheet import make_sheet, svg_sheet

TO_WGS = Transformer.from_crs(5179,4326,always_xy=True).transform
SITE = mapping(transform(TO_WGS,box(953000,1950000,953130,1950120)))
THEME = mapping(transform(TO_WGS,box(953015,1950010,953090,1950080)))


def _snapshot():
    return {
        'geometry':SITE,
        'print_scale':{'mode':'FIXED','denominator':1000,'label':'1:1,000'},
        '_cad_background':{'status':'COMPLETE','features':[
            {'type':'Feature','geometry':SITE,'properties':{'jibun':'100-1'}}],
            'request_count':4,'coverage_assurance':'PROVIDER_RESPONSE_ONLY'},
    }


class CADA3SheetAcceptanceTest(unittest.TestCase):
    def test_subject_sheet_has_both_real_cad_frames_and_legend(self):
        data={
            'id':'2-2','name':'지목별','provider_verified':True,
            'thematic_type':'jimok','status':'공간자료 확보',
            'legend':[
                {'name':'대','color':'#f0bfa9','class_code':'C01'},
                {'name':'전','color':'#77bba9','class_code':'C02'},
            ],
            'features':[{'type':'Feature','geometry':THEME,
                'properties':{'name':'대','color':'#f0bfa9','class_code':'C01'}}],
        }
        snap=_snapshot()
        ops=make_sheet(data,snap)
        self.assertGreater(sum(1 for op in ops if op['type']=='rect'),6)
        self.assertTrue(any(op.get('dash')=='two_dot_chain' and
                            op.get('layer')=='SHEET_SITE' for op in ops))
        self.assertTrue(any(op.get('text')=='확정 구역계 (2점쇄선)' for op in ops))
        self.assertTrue(any(op.get('text')=='범 례' for op in ops))
        self.assertTrue(any(op.get('text')=='대' for op in ops))
        self.assertTrue(any(op.get('text')=='전' for op in ops))
        svg=svg_sheet(ops)
        self.assertIn(b'11 3 0.5 2 0.5 3',svg)

        meta={}
        raw=dxf_bytes(data,snap,meta)
        doc=ezdxf.read(io.StringIO(raw.decode('utf-8')))
        self.assertEqual(doc.layouts.active_layout().name,'SURVEY_A3')
        self.assertEqual(int(doc.header['$TILEMODE']),0)
        self.assertEqual(meta['opens_in_layout'],'SURVEY_A3')
        self.assertIn('model_companion_sheet',meta)

        layout=doc.layouts.get('SURVEY_A3')
        paper_texts=[e.dxf.text for e in layout.query('TEXT')]
        self.assertIn('지목별',paper_texts)
        self.assertIn('범 례',paper_texts)
        self.assertIn('확정 구역계 (2점쇄선)',paper_texts)
        self.assertIn('N',paper_texts)
        self.assertIn('1:1,000',paper_texts)
        self.assertIn('대',paper_texts)
        self.assertIn('전',paper_texts)
        paper_site=list(layout.query('LWPOLYLINE[layer=="SHEET_SITE"]'))
        self.assertTrue(paper_site,'site boundary not present as editable CAD object')
        self.assertTrue(all(e.dxf.linetype=='SURVEY_CHAIN2_PAPER' for e in paper_site))
        self.assertIn('SURVEY_CHAIN2_PAPER',doc.linetypes)
        self.assertTrue(list(layout.query('HATCH')),'thematic and legend colors must be real hatches')
        self.assertTrue(list(layout.query('LWPOLYLINE[layer=="SHEET_FRAME"]')),
                        'paper frame must be a real closed CAD polyline')

        model=doc.modelspace()
        source=list(model.query('LWPOLYLINE[layer=="SITE_BOUNDARY"]'))
        self.assertTrue(source,'georeferenced site boundary missing')
        self.assertEqual(doc.layers.get('SITE_BOUNDARY').dxf.linetype,'SURVEY_CHAIN2_MODEL')
        self.assertTrue(all(min(v[0] for v in e.get_points('xy'))>900000 for e in source))
        model_text=[e.dxf.text for e in model.query('TEXT')]
        self.assertIn('지목별',model_text)
        self.assertIn('구역계 (2점쇄선)',model_text)
        self.assertIn('대',model_text)
        self.assertIn('전',model_text)
        self.assertTrue(list(model.query('LWPOLYLINE[layer=="MODEL_SHEET_FRAME"]')))
        self.assertTrue(list(model.query('HATCH[layer=="MODEL_SHEET_LEGEND"]')))
        self.assertEqual(doc.audit().has_errors,False)

    def test_legend_appears_on_unclassified_location_sheet(self):
        item={'id':'1-1','name':'위치 및 면적','features':[],'legend':[]}
        ops=make_sheet(item,_snapshot())
        self.assertTrue(any(op.get('text')=='확정 구역계 (2점쇄선)' for op in ops))
        self.assertTrue(any(op.get('text')=='주변 연속지적' for op in ops))
        doc=ezdxf.read(io.StringIO(dxf_bytes(item,_snapshot(),{}).decode('utf-8')))
        self.assertIn('SURVEY_A3',doc.layouts)
        self.assertTrue(list(doc.modelspace().query('LWPOLYLINE[layer=="MODEL_SHEET_FRAME"]')))


if __name__=='__main__':
    unittest.main()
