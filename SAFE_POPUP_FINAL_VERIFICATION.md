# v2.5.0 r22 안심주택 팝업 오류 최종 보완

- 기준: 공장 + 도로 최종본 유지
- 앱 버전: v2.5.0 유지
- 변경 범위: 안심주택 상세검토 팝업 호출/렌더 안정화만 수정

## 수정
1. 안심주택 클릭만 전용 `openSafeHousingDetailSafely()` 경로로 분리.
2. 모달과 로딩 안내를 먼저 표시한 뒤 상세 렌더 실행.
3. 렌더 오류가 나도 `renderSchemePopupFallback('safe', e)`로 팝업 자체 유지.
4. `renderSafeHousingDetailPopup()`에서 `buildSiteFactStore()` 재실행 제거.
   - 기존 분석 Fact만 읽음.
   - 분석 전이면 `현황분석 필요` 안내.
   - 목적사업 필터 상태에서도 팝업은 열리고 필터 사유를 표시.
5. 다른 사업방식의 팝업 호출 경로는 변경하지 않음.

## 직접 검증
- Node `--check`: PASS
- Python regression 파일 compile: PASS
- 강제 렌더 오류 DOM 흐름 테스트: 모달 open 유지 + fallback 표시 PASS
- 안심주택 렌더러 내부 `buildSiteFactStore()` 미호출 정적검사 PASS
