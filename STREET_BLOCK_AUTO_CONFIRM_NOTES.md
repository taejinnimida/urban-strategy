# 가로구역 자동확정 — 수기확인 제거, AUTO 품질 기반으로 전환

## 방향 수정
이전 패치에서 "전문가가 매번 인정 버튼을 눌러야 확정"으로 만들었는데, 이건
님 의도와 반대였다. **실제 요구**: 가로구역 산정 알고리즘 자체가 이미 검증된
방법론이므로, 계산이 깨끗하게 성공(AUTO 품질)했으면 그 결과를 바로 자동으로
신뢰 근거로 써야 한다 — 사람이 매번 개입하는 별도 판정 단계는 없어야 한다.

## 변경
- `street_block_expert_confirm` 수기입력 UI 완전 제거
- `streetBlockIsAuthoritative()` — 이제 `streetBlockAnalysis.quality==='AUTO'`이면
  자동으로 true. AUTO는 기초단위구 병합·도로장벽·시설장벽 조회가 전부 에러 없이
  끝난 상태(기존 코드에 이미 있던 품질 신호, 새로 안 만듦). 일부 조회가 실패한
  PARTIAL 품질은 여전히 REVIEW.
- 공식 데이터 경로(`metadata.authoritative_street_block`)는 그대로 최우선 유지 —
  실제로 생기면 그게 여전히 이김.
- 표시 문구를 "공식"/"가로구역 자동확정"/"추정" 3단계로 정리.

## 효과
역세권활성화·역세권복합개발 등 `streetBlockIsAuthoritative()`를 쓰는 5곳 이상이
전부, 가로구역 계산이 AUTO 품질로 성공하면 자동으로 REVIEW를 넘어가서 실제
겹침 비율(%)에 따라 PASS/FAIL이 결정된다.

## 검증
- `node --check` PASS
- 로직 시뮬레이션 4케이스(AUTO/PARTIAL/NONE/공식데이터가정) 전부 의도대로 확인
- `regression_checks.py` 전체 **50개 PASS**(사람 개입 UI가 없다는 것도 회귀테스트로 고정)
