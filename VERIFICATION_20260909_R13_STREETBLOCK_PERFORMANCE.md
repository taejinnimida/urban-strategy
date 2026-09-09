# R13 가로구역 성능 개선 검증

## 현상
단순한 직사각형 대상지에서도 제도별 가로구역 분석 완료까지 약 5분이 소요됨. 기존 코드는 각 제도마다 항상 700m 탐색범위를 사용하고, 공간규칙이 동일한 역세권활성화/역세권복합개발도 각각 백엔드 연산함.

## 변경
- 1차 탐색반경 320m.
- `frame_boundary_touched=true` 또는 `merge_limit_reached=true`인 경우에만 700m 재검증.
- backend 응답 metadata에 `analysis_radius_m`, `frame_boundary_touched` 추가.
- 역세권활성화와 역세권복합개발의 공간규칙이 실제로 동일한 경우에만 activation 블록 결과 재사용.
- 가로구역 선행·순차실행 원칙 유지. background 병렬화 없음.
- 단계별 소요시간 Console 계측 추가.

## 검증
- `python -m py_compile app.py`: PASS
- 전체 inline JavaScript `node --check`: PASS
- R5 targeted: PASS
- R6 targeted: PASS
- R8 targeted: PASS
- R9 targeted: PASS
- R10 targeted: PASS
- R12 targeted: PASS
- R13 targeted: PASS

## 성능 기대 근거
320m 탐색창의 면적은 700m 탐색창의 약 21% 수준이다. 단순 블록은 1차 탐색에서 닫히므로 기초단위구 후보 수와 공간교차량이 크게 줄어든다. 복잡하거나 큰 블록은 자동으로 700m 재검증되어 정확성 우선 원칙을 유지한다. 또한 동일한 4m 공간규칙을 두 번 계산하던 1회가 제거된다.

실제 소요시간은 배포환경에서 Console의 `[가로구역 성능]` 로그로 확인해야 하며, 320m 단계에서도 비정상적으로 오래 걸리면 다음 단계로 서버 내부 공통 전처리(batch/shared context) 최적화를 적용한다.
