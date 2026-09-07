# r48 사이트 분석 UI 긴급 핫픽스 1

## 범위
- 기준본: `urban-strategy-v2_5_0-r48-site-analysis-ui.zip`
- 내부 앱 버전/판정기준/공간분석 로직은 변경하지 않음.
- 중복 현황 UI 비노출 + UI 재편 후 남은 JS DOM 참조 안전화만 수행.

## 변경
1. 사이트 분석 상단의 중복 `토지/건축물/역세권/중심지` 요약 카드 영역을 화면에서 제거(엔진 동기화용 DOM은 숨김 유지).
2. `updateRedevelopmentStrategySignal()`의 모든 출력 DOM 참조를 null-safe 처리.
3. UI 재편으로 사라진 레거시 JS hook은 숨김 compatibility DOM으로 유지:
   - `lyFrontagePass`, `lyFrontageFail`, `lyRoad6`
   - `strategyTotalBuildings`, `strategyOldBuildings`, `strategyOldRatio`, `strategySmallCount`, `strategySmallRatio`, `strategySignal`, `strategyAgeJudgement`, `strategyReason`
   - `ovPlanRenewal`, `ovPlanPromotion`, `ovSummaryFoot`
4. `serviceSchemeCompareBtn`은 기존대로 런타임 동적 생성되므로 변경 없음.

## 전수 점검
- HTML `id="..."`: 532개
- JS literal `getElementById('...')`: 200개 고유 참조
- JS가 찾지만 HTML에 없는 literal ID: **0개**
- 기존 중복 ID 4종(`mobileDecisionSection`, `RENEWAL_GIS`, `RENEWAL_ORD`, `RENEWAL_ACT`)은 r47에도 존재하던 기존 구조로 이번 핫픽스에서 변경하지 않음.

## 검증
- `python regression_checks.py`: **전체 PASS**
- 전체 inline JS `node --check`: **PASS**
- `updateRedevelopmentStrategySignal()` DOM 부재/존재 시뮬레이션:
  - DOM 전부 부재: 예외 없이 return
  - compatibility DOM 존재: 10동/7동/70.0%, 과소필지 25.0%, `재개발 검토 우선` 정상 기록

## 제외
- `buildIndependentProjectAreaCandidate()` R42 정교화 작업은 본 긴급 UI 핫픽스와 섞지 않음. 별도 패치로 진행.
