# HANDOFF — R6 용도지역 핵심 FACT 안정화

베이스: `urban-strategy-v2.5.0-20260918-r40-REGULATORY-WIP-ANALYSIS-STABILITY-R5.zip`

이번 변경은 용도지역 LT_C_UQ111의 0건/미확보 상태를 성공으로 오인하던 상태판정 오류만 수정한다. 사업별 법적 RULE은 수정하지 않았다.

핵심: 도시계획 GIS 전체 성공 여부와 용도지역 핵심 FACT 확보 여부를 분리하고, 용도지역 미확보 시 UQ111만 단독 재확인한다. 실패 시 사업판정은 보류 상태로 표시한다.
