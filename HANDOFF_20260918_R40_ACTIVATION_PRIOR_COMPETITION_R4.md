# HANDOFF — R40 역세권활성화·사전협상 경합전략 R4

## 기준
- 앱 내부 버전: v2.5.0 유지
- 직접 작업기준: `20260918-r40-REGULATORY-WIP-URBAN-REGEN-INTEGRATED-R3`
- canonical `20260917-r40-INNOVATION-2TYPE-POPUP3-ONLY` 원본 바이트는 현재 세션에서 접근 제한 상태이므로 본 파일은 canonical 승격본이 아니라 WIP 계열 병합후보임.

## 핵심 변경
- 역세권활성화/사전협상 기존 법적 판정함수 보존
- 두 제도의 5,000㎡ 경합을 별도 전략 RESULT로 추가
- 경합 대상은 빨간색 조건부 추진후보로 상단 노출
- 역세권활성화의 역세권형/간선가로형 경로를 후보 UI에 표시
- 간선가로형에는 기존 노선형 상업지역 분석 FACT를 표시
- 선택필지 1개 제외로 5,000㎡ 미만이 되는 시나리오가 있으면 구역계 조정 가능성을 안내
- 추천/AI에는 전략경합 FACT를 전달하되 raw PASS/FAIL/REVIEW를 변경하지 않음

## 반드시 보존할 것
- R3 김포공항 도시재생혁신지구 SHP 통합
- retry13: rejected만 마지막에 1회 순차 재분석
- 기존 사업판정/밀도/접도/가로구역/사업구역/POPUP3

## 검증
- `regression_check_activation_prior_competition_r4.py`: 30/30 PASS
