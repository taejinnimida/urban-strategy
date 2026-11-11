"""Offline regression for WMS-only land-cover reference rendering."""
import io
import unittest
from PIL import Image
from shapely.geometry import Polygon, mapping
from development_landcover_preview import clipped_png, site_mask, reference_svg


def png(color=(238, 72, 81, 255)):
    image = Image.new('RGBA', (1024, 768), color)
    stream = io.BytesIO()
    image.save(stream, format='PNG')
    return stream.getvalue()


SITE = mapping(Polygon([
    (127.001, 37.001), (127.009, 37.001),
    (127.009, 37.009), (127.001, 37.009)
]))


class LandcoverWmsPreviewTests(unittest.TestCase):
    def test_site_mask_clips_outside_and_preserves_inside(self):
        raw = png()
        result = clipped_png(raw, SITE)
        with Image.open(io.BytesIO(result)) as image:
            self.assertEqual(image.getpixel((0, 0))[3], 0)
            self.assertEqual(image.getpixel((512, 384)), (238, 72, 81, 255))
            self.assertEqual(image.size, (1024, 768))
            self.assertEqual(image.format, 'PNG')

    def test_hole_in_site_is_transparent(self):
        holed = mapping(Polygon(
            [(127.001,37.001),(127.009,37.001),
             (127.009,37.009),(127.001,37.009)],
            holes=[[(127.004,37.004),(127.006,37.004),
                    (127.006,37.006),(127.004,37.006)]]))
        mask = site_mask(1024, 768, holed)
        self.assertEqual(mask.getpixel((512,384)), 0)
        self.assertEqual(mask.getpixel((400,384)), 255)

    def test_reference_sheet_has_frame_boundary_reference_notice(self):
        wms = {'status': 'MAP_ONLY', '_image': png()}
        svg = reference_svg(SITE,wms).decode('utf-8')
        self.assertIn('viewBox="0 0 580 380"',svg)
        self.assertIn('토지피복별 현황',svg)
        self.assertIn('확정 대상지',svg)
        self.assertIn('WMS 원영상 참고도',svg)
        self.assertIn('면적·구성비: 미산정',svg)
        self.assertIn('data:image/png;base64,',svg)

    def test_no_unsupported_area_calculation_from_raster(self):
        with self.assertRaises(ValueError):
            reference_svg(SITE, {'status':'ERROR','_image':png()})

if __name__ == '__main__':
    unittest.main()
