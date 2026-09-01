# R22 Station Line Direct Probe Fix

## 문제 원인

- `stations.json` 349개 역사 폴리곤 중 255개가 `station_lines=[]` 상태이며, 현재 테스트 대상의 한양대역·왕십리역·응봉역도 내장 노선정보가 비어 있다.
- 따라서 외부 공식 역-노선 참조가 실패하면 VWorld 보조검색만 남고, 왕십리역도 환승결절로 확정되지 않았다.
- 이전 버전은 전역 `/api/reference/station-lines` 호출 하나에 지나치게 의존했고, 한 개 노선만 공식표에서 확인되어도 `비환승`으로 확정할 수 있는 구조였다. 서울교통공사 자료는 다른 운영기관 노선을 모두 포괄하지 않으므로 이 판정은 안전하지 않다.

## 수정

1. `CardSubwayStatsNew`를 1차 전역 노선 보강자료로 사용한다.
   - 최근 3~7일 범위에서 최대 5회만 시도한다.
   - 기존 12회 x 20초의 장기 직렬대기를 제거하고 호출당 timeout을 8초로 축소했다.
   - 경의선/중앙선→경의중앙선, 분당선/수인선→수인분당선, 경부·경인·경원·장항선→1호선 등 승객 기준 노선으로 정규화한다.
2. `SearchSTNBySubwayLineInfo`는 서울교통공사 노선 보강자료로 사용한다.
   - 왕십리 2호선+5호선처럼 이 자료 하나에서 2개 이상이 확인되면 환승결절을 바로 확정할 수 있다.
3. 전역 노선표에서 환승이 확인되지 않은 1km 후보역은 `/api/reference/station-line/{station_name}`으로 역명 직접조회한다.
   - `SearchInfoBySubwayNameService`를 사용한다.
   - 왕십리역에서 2호선+5호선이 확인되면, 경의중앙·수인분당 보강 여부와 관계없이 성장거점형의 `2개 이상 철도노선 교차 환승역`을 확정한다.
4. `1개 노선 확인 = 비환승`으로 자동 확정하지 않는다.
   - 운영기관 누락 가능성이 있으므로 2개 이상이 공식 확인된 경우만 `transfer=true`로 확정한다.
   - 그 외에는 `transfer=null / REVIEW`를 유지한다.
5. 성장거점형 및 환승역을 요구하는 Rule은 `line_count>=2`가 아니라 `transfer===true`를 사용한다.
   - VWorld 보조검색에서 우연히 여러 노선 문자열이 잡혀도 법정 환승결절로 자동 PASS하지 않는다.
6. `/api/reference/station-lines` metadata에 `wangsimni_probe`를 추가한다.
   - 화면의 역사 데이터 상태문에서 왕십리 노선 수를 바로 확인할 수 있다.

## 검증

- Python compile PASS
- JavaScript `node --check` PASS
- 기존 v2.5.0 regression 전체 PASS 출력 확인
- 추가 회귀검사:
  - Card API 실패 + SearchSTN 성공 → 왕십리 `2호선·5호선`, 환승 TRUE
  - SearchSTN 실패 + Card 성공 → 왕십리 `2호선·5호선·경의중앙선·수인분당선`, 환승 TRUE
  - 전역표 실패 + 역명 직접조회 성공 → 왕십리 `2호선·5호선`, 환승 TRUE

## 남은 한계

- 현재 내장 역사 geometry의 노선속성 자체가 불완전하므로 외부 공식 노선 API를 완전히 제거한 상태는 아니다.
- 장기적으로는 서울 전 역사-노선 공식 master snapshot을 배포본에 함께 넣고, OpenAPI는 갱신용으로 사용하는 구조가 가장 안정적이다.
