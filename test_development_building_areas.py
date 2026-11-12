"""Regression tests: building site-area references, audited derived ratios, and 2-5 exclusion."""
from datetime import date
from shapely.geometry import box, mapping
from development_report import buildings, review
from development_building_theme import attach_building_themes

PNU = '1121510100101640005'


def _title(**changes):
    row = {
        'mgmBldrgstPk': 'TEST001', 'platPlc': '서울특별시 중곡동 164-5',
        'mainPurpsCdNm': '제1종근린생활시설',
        'grndFlrCnt': 4, 'ugrndFlrCnt': 1,
        'archArea': 80, 'totArea': 440,
        'bcRat': 0, 'vlRat': 0, 'platArea': 0, 'vlRatEstmTotArea': 320,
    }
    row.update(changes)
    return row


def _land(area=160, status='확보'):
    return {PNU: {'pnu': PNU, 'official_area': area, 'ledger_status': status}}


def test_derived_ratios_use_documented_title_area_and_lot_reference():
    result = buildings({PNU: [_title()]}, date(2026, 11, 11), _land())[0]
    assert result['site_area'] == 160
    assert result['site_area_source'] == '토지대장 공부면적(건축대지 여부 별도 확인)'
    assert result['coverage'] == 50
    assert result['far'] == 200
    assert '플랫폼 내부산정' in result['coverage_basis']
    assert '용적률산정용 연면적' in result['far_basis']


def test_positive_register_ratios_take_priority():
    row = buildings({PNU: [_title(bcRat=49.34, vlRat=199.07, platArea=200)]},
                    date(2026, 11, 11), _land())[0]
    assert row['coverage'] == 49.34
    assert row['far'] == 199.07
    assert row['coverage_basis'] == '건축HUB 원문'
    assert row['far_basis'] == '건축HUB 원문'
    assert row['site_area_source'] == '건축HUB 대지면적'


def test_missing_site_area_is_not_invented():
    row = buildings({PNU: [_title()]}, date(2026, 11, 11), _land(status='미확보'))[0]
    assert row['site_area'] is None
    assert row['coverage'] is None
    assert row['far'] is None


def test_rough_floor_based_far_stays_explicitly_nonstatutory():
    row = buildings({PNU: [_title(vlRatEstmTotArea=0)]}, date(2026, 11, 11), _land())[0]
    assert row['far'] == 200
    assert '개략추정' in row['far_basis']


def test_provisional_map_does_not_label_calculation_as_register_original():
    title = buildings({PNU: [_title()]}, date(2026, 11, 11), _land())[0]
    feature = {'type': 'Feature', 'geometry': mapping(box(1, 1, 3, 3)),
               'properties': {}}
    data = {
        'as_of': '2026-11-11', 'buildings': [title],
        'items': [{'id': f'3-{i}', 'name': str(i), 'detail': ''} for i in range(1, 8)],
        'statistics': {},
    }
    snapshot = {
        'geometry': mapping(box(0, 0, 10, 10)),
        '_report_layers': {
            'LT_C_BLDGINFO': {'status': 'COMPLETE', 'features': [feature]},
            'LT_C_SPBD': {'status': 'COMPLETE', 'features': []},
            'LP_PA_CBND_BUBUN': {
                'status': 'COMPLETE',
                'features': [{'type': 'Feature', 'id': PNU,
                              'geometry': mapping(box(0, 0, 4, 4)),
                              'properties': {'pnu': PNU}}]
            },
        },
    }
    attach_building_themes(data, snapshot)
    record = data['building_fact']['records'][0]
    assert record['attributes']['coverage']['value'] == 50
    assert record['attributes']['coverage']['assurance'] == 'INTERNAL_CALC_PROVISIONAL'
    assert record['attributes']['far']['assurance'] == 'INTERNAL_CALC_PROVISIONAL'
    # A cadastral PNU is not a building outline: keep calculations in the
    # ledger table, but never fill the parcel as a building on map/CAD.
    assert data['items'][5]['features'] == []
    assert data['items'][5]['survey_table']['rows'][0]['count'] == 1
    assert data['items'][5]['survey_table']['rows'][2]['count'] == 1


def test_two_five_is_not_an_active_survey_item():
    snapshot = {'geometry': mapping(box(0, 0, 10, 10)), 'area_m2': 100,
                '_report_layers': {}}
    report = {'as_of': '2026-11-11T00:00:00', 'land': {},
              'buildings': {}}
    items = review(report, snapshot)
    assert not any(item['id'] == '2-5' for item in items)
    assert len(items) == 43
