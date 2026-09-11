from pathlib import Path

py = Path('app.py').read_text(encoding='utf-8')
html = Path('app.html').read_text(encoding='utf-8')

checks = []
def check(name, cond):
    if not cond:
        raise AssertionError(name)
    print('PASS', name)
    checks.append(name)

check('planplus full project map added', 'PLANPLUS_PROJECT_TYPES' in py)
for code in ['BZ301','BZ302','BZ303','BZ306','BZ502','BZ201','BZ202','BZ401','BZ402','BZ601','BZ604']:
    check(f'planplus code {code} retained', f"'{code}'" in py or f'"{code}"' in py)
check('planplus stage code map added', 'PLANPLUS_STAGE_LABELS' in py and 'PP0808' in py and '사업계획승인' in py)
check('create dat not used as notice date', '"notice_date": str(row.get("CREATE_DAT")' not in py)
check('planplus reference date explicit', 'data_reference_date' in py and 'CREATE_DAT은 고시일이 아닌 데이터 생성일' in py)
check('planplus current stage fields exposed', 'project_stage_code' in py and 'project_stage_label' in py)
check('planplus independent spatial index', 'def _planplus_project_spatial_index' in py)
check('renewal rule overlap kept separate', 'project_registry_overlaps' in py and '"overlaps": overlaps' in py)
check('existing project legend ui', 'id="spExistingProjectLegend"' in html and '기존 고시·추진사업' in html)
check('project registry stored separately in frontend', 'projectRegistryOverlaps' in html and 'projectRegistryContextFeatures' in html)
check('project registry does not replace rule overlaps', 'existing_projects' in html and 'renewal_area_type' in html)
check('current stage rendered in legend', 'project_stage_label' in html and '현재단계' in html)
check('map displays legal plus full planplus registry', 'legalContext' in html and 'projectContext' in html and 'displayOverlap' in html)
check('history limitation stated', '단계별 고시·승인일 이력은 후속 결정고시 연계 전까지 현재 추진단계만 표시' in html)
check('R28 frontend performance fix retained', 'frontend_postprocess_ms' in html)
check('R27 candidate alternative retained', '대안추천' in html)
check('R24 parcel click retained', 'parcelFindMode' in html)
print(f'ALL R29 TARGETED CHECKS PASS ({len(checks)}/{len(checks)})')
