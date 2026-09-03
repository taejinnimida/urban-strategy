const fs = require('fs');
const vm = require('vm');

const html = fs.readFileSync('app.html', 'utf8');

function sourceBetween(startName, endName) {
  const start = html.indexOf(`function ${startName}(`);
  const end = html.indexOf(`function ${endName}(`, start + 1);
  if (start < 0 || end < 0) throw new Error(`function source missing: ${startName}`);
  return html.slice(start, end);
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

// 안심주택 역세권 판정 분기: 250m는 PASS, 350m는 REVIEW, 연결 불완전 시 FAIL 금지.
const stationState = { base: [], safe350: [] };
const stationContext = {
  console,
  stationAnalysis: { loaded: true },
  stationEntranceReferenceStatus: { loaded: true, linkage_complete: false, note: '공식 연결키 없음' },
  stationFacts: () => stationState.base,
  bestStationByCoverage: (_m, predicate) => stationState.base.filter(predicate)[0] || null,
  bestSafe350: predicate => stationState.safe350.filter(predicate)[0] || null,
  bestStationByDistance: () => stationState.base[0] || null,
  safeStationDisplayMetrics: st => ({
    geometry_basis: '승강장 경계', coverage250: st?.coverage250 ?? null,
    safe350_basis: '승강장 경계 + 해당 역 연결 출입구',
    safe350_coverage: stationState.safe350.find(x => x.name === st?.name)?.safe350_coverage ?? null,
    entrance_count: stationState.safe350.find(x => x.name === st?.name)?.safe350_entrance_count || 0,
    entrance_loaded: stationContext.stationEntranceReferenceStatus.loaded,
    linkage_complete: stationContext.stationEntranceReferenceStatus.linkage_complete,
    linkage_note: stationContext.stationEntranceReferenceStatus.note,
  }),
  fmtSchemePct: v => v == null ? '-' : `${Number(v).toFixed(1)}%`,
};
vm.createContext(stationContext);
vm.runInContext(sourceBetween('safeStationPath', 'safeArterialPath'), stationContext);

stationState.base = [{ name: '기준역', coverage250: 55, distance_m: 100 }];
stationState.safe350 = [{ name: '기준역', safe350_coverage: 80, safe350_entrance_count: 4, distance_m: 100 }];
let result = stationContext.safeStationPath({});
assert(result.status === 'PASS' && result.scope_m === 250, '250m 과반은 일반경로 PASS여야 함');

stationState.base = [{ name: '기준역', coverage250: 0, distance_m: 300 }];
stationState.safe350 = [{ name: '기준역', safe350_coverage: 60, safe350_entrance_count: 4, distance_m: 300 }];
result = stationContext.safeStationPath({});
assert(result.status === 'REVIEW' && result.scope_m === 350, '출입구 포함 350m 과반은 REVIEW여야 함');

stationState.safe350 = [{ name: '기준역', safe350_coverage: 51, platform_only_coverage: 40, safe350_entrance_count: 4, distance_m: 300 }];
result = stationContext.safeStationPath({});
assert(result.status === 'REVIEW' && result.coverage === 51, '출입구 합산으로 과반이 되어도 자동 PASS 금지');

stationState.safe350 = [{ name: '기준역', safe350_coverage: 0, safe350_entrance_count: 0, distance_m: 300 }];
stationContext.stationEntranceReferenceStatus.loaded = true;
stationContext.stationEntranceReferenceStatus.linkage_complete = false;
result = stationContext.safeStationPath({});
assert(result.status === 'REVIEW', '출입구 연결 불완전 시 자동 FAIL 금지');

stationContext.stationEntranceReferenceStatus.linkage_complete = true;
result = stationContext.safeStationPath({});
assert(result.status === 'FAIL', '공식 연결 완료 후 전 범위 비중첩만 FAIL 가능');

// 다른 역의 출입구는 정확한 역명 키가 다르면 포함하지 않는다.
const entranceContext = {
  stationEntranceMap: new Map([['A역', [{ lon: 127, lat: 37.5, entrance_id: 'A-1' }]]]),
  turf: { point: (coords, properties) => ({ type: 'Feature', geometry: { type: 'Point', coordinates: coords }, properties }) },
};
vm.createContext(entranceContext);
vm.runInContext(sourceBetween('safeEntranceFeaturesForStation', 'safeStation350Geometry'), entranceContext);
assert(entranceContext.safeEntranceFeaturesForStation({ name: 'A역' }).length === 1, '해당 역 출입구 누락');
assert(entranceContext.safeEntranceFeaturesForStation({ name: 'B역' }).length === 0, '다른 역 출입구 혼입');

// 안심주택도 다른 사업과 동일한 공통 팝업 경로를 사용한다.
let openCount = 0;
let routedName = '';
const openerContext = {
  console,
  openReviewModal: name => { if (name === 'schemeDetailModal') openCount += 1; },
  renderSchemeDetailPopup: name => { routedName = name; },
  renderSchemePopupFallback: () => { throw new Error('unexpected popup fallback'); },
};
vm.createContext(openerContext);
vm.runInContext(sourceBetween('openSchemeDetailSafely', 'showSmallscaleRouteBasis'), openerContext);
openerContext.openSchemeDetailSafely('safe');
assert(openCount === 1 && routedName === 'safe', '안심주택이 공통 팝업 경로로 열리지 않음');

let commonRouteName = '';
const showContext = {
  console, activeSmallscaleRoute: 'x',
  document: { querySelectorAll: () => [] },
  renderMobileSelectedScheme: () => {},
  openSchemeDetailSafely: name => { commonRouteName = name; },
};
vm.createContext(showContext);
vm.runInContext(sourceBetween('showCandidateBasis', 'scrollToSchemeDetail'), showContext);
showContext.showCandidateBasis('safe');
assert(commonRouteName === 'safe', '안심주택 카드가 공통 팝업 함수를 호출하지 않음');

// '결과 정교화 — 선택사항'이 비어 있어도 안심주택 Rule Fact는 생성·보존한다.
let evaluationCount = 0;
const moduleContext = {
  console, schemeNames: { safe: '안심주택' }, schemeResults: {}, latestSchemeModuleResults: {}, activeRuleScheme: '',
  purposeEngineGate: () => ({ enabled: false, reason: '주거(임대) 선택 시 추천 반영' }),
  schemeRunIssue: () => ({}), schemeModuleReviewResult: () => ({ overall: 'REVIEW' }),
};
vm.createContext(moduleContext);
vm.runInContext(sourceBetween('evaluateSchemeModulesSafely', 'runAllSchemeChecks'), moduleContext);
const optionFreeStore = { scheme_specific: {} };
moduleContext.evaluateSchemeModulesSafely(optionFreeStore, ['safe'], (_name, store) => {
  evaluationCount += 1;
  store.scheme_specific.safe = { generated: true };
  return { overall: 'REVIEW', rows: [], facts: store.scheme_specific.safe };
});
assert(evaluationCount === 1, '선택사항 미입력 시 안심주택 Rule Module이 실행되지 않음');
assert(optionFreeStore.scheme_specific.safe?.generated, '선택사항 미입력 시 안심주택 Fact가 삭제됨');
assert(moduleContext.schemeResults.safe?.purpose_refinement_required === true, '추천 정교화 조건 표시 누락');

// 실제 상세 렌더 함수가 출입구·250m·350m 값을 오류 없이 출력하는지 확인한다.
const popupBody = { innerHTML: '' };
const popupTitle = { textContent: '' };
const popupFacts = {
  location: {
    selected: { label: '대중교통 중심지역 · 역세권' }, coverage_pct: 72.1,
    station: {
      value: '판정역 왕십리역', station: { name: '왕십리역' }, status: 'REVIEW', scope_m: 350,
      geometry_basis: '승강장 경계', coverage250: 0, safe350_basis: '승강장 경계 + 해당 역 연결 출입구',
      safe350_coverage: 72.1, entrance_loaded: true, entrance_count: 6, linkage_complete: false,
      note: '350m 예외지정/통합심의 필요',
    },
    arterial: { value: '-' }, medical: { value: '-' }, district_plan: { known: false },
  },
  zoning: { current: '제2종일반주거' }, area: { m2: 5000 }, aging: { assessment: { value: '60%' } },
  planning: { density: { zone: '준주거', far: '400%', contribution: '-' } },
};
const popupContext = {
  document: { getElementById: id => id === 'schemeDetailPopupTitle' ? popupTitle : id === 'schemeDetailPopupBody' ? popupBody : null },
  schemeResults: { safe: { overall: 'REVIEW', rows: [], facts: popupFacts, purpose_refinement_required: true, purpose_refinement_reason: '주거(임대) 선택 시 추천 반영' } },
  latestSchemeModuleResults: {}, latestSiteFactStore: { site: { address: '서울시 테스트' }, scheme_specific: { safe: popupFacts } }, analysisState: {},
  escHtml: v => String(v ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
  fmtSchemeArea: v => v == null ? '-' : `${v}㎡`, fmtSchemePct: v => v == null ? '-' : `${Number(v).toFixed(1)}%`,
  ageFactValue: a => a?.value || '-', schemeSpecificResultRows: () => '', schemeSpecificPlanRow: () => '',
  schemeSheetSourceFor: () => '', schemeSheetSteps: () => '', schemeDetailSummary: () => '', schemeSheetFeasibility: () => '조건부 검토',
};
vm.createContext(popupContext);
vm.runInContext(sourceBetween('renderSafeHousingDetailPopup', 'renderSharedHousingDetailPopup'), popupContext);
popupContext.renderSafeHousingDetailPopup();
assert(popupBody.innerHTML.includes('250m 일반경로 포함률'), '팝업 250m 포함률 누락');
assert(popupBody.innerHTML.includes('350m 예외 포함률'), '팝업 350m 포함률 누락');
assert(popupBody.innerHTML.includes('72.1%') && popupBody.innerHTML.includes('출입구 연결'), '팝업 출입구 Fact 출력 누락');
assert(popupBody.innerHTML.includes('결과 정교화 선택사항'), '선택사항 안내와 기초검토서가 함께 표시되지 않음');

console.log('base review independent of options + safe housing popup + entrance 350m regression: PASS');
