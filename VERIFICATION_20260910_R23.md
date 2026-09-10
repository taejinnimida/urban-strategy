# VERIFICATION_20260910_R23

## 기준본
- `urban-strategy-v2.5.0-20260910-streetblock-prepared-r22-PATCH.zip`
- 앱 내부 버전 `v2.5.0` 유지
- R19 도로 100m, R20 가로구역 면적 선필터, R21 접도율 법령정합, R22 prepared geometry 모두 보존

## 목적
가로구역 flood-fill에서 동일 기초단위구 인접쌍에 대해 `STRtree query → boundary.intersection() → barrier 판정`을 seed·pass·제도마다 반복하는 비용을 줄인다. Rule·임계값·최종 판정/도형은 변경하지 않는다.

## R23 변경
1. `_ensure_basic_unit_neighbors()` 추가
   - 전체 750m 범위를 선계산하지 않는다.
   - flood-fill이 실제 방문한 기초단위구만 STRtree 조회 및 공유경계 계산한다.
   - 계산된 neighbor order / shared boundary는 공통 context에 캐시하여 이후 seed·component pass·다른 제도에서 재사용한다.
   - 기존 `tree.query()` 결과 순서를 보존하여 `max_units=240` 조기종료 시 방문순서 변화 위험을 최소화한다.

2. `_basic_unit_component_graph()` 추가
   - 정적 인접관계를 재사용하는 BFS.
   - `road_union/strong_union`이 고정된 한 component pass 안에서는 `(i,j) → blocked` 결과를 최초 1회만 계산하고 다른 seed가 재사용한다.
   - barrier가 변경되는 다음 pass에서는 새 cache를 사용하므로 R22/R13 판정 의미를 유지한다.

3. R22 prepared geometry 유지
   - `.intersects()`는 prepared predicate 사용.
   - `.intersection().area`는 raw geometry 사용.

4. 성능 진단 metadata 추가
   - `neighbor_graph=true`
   - `neighbor_graph_mode=lazy`
   - `neighbor_edge_count`
   - `neighbor_query_count`
   - `neighbor_shared_calc_count`
   - `neighbor_topology_ms`
   - `neighbor_materialized_node_count`
   - `neighbor_cache_hits`
   - `component_barrier_evaluations[]`
   - `component_barrier_cache_hits[]`
   - 기존 R22 `component_pass_ms[]`, `prepared_build_ms[]`, `rule_apply_ms` 유지

## 중요한 설계 변경: full prebuild → lazy graph
초기 구현에서 750m 로컬 기초단위구 전체 adjacency를 선구축하면, 작은 사이트/seed 1개에서는 방문하지 않을 외곽 unit까지 계산하여 역효과가 발생했다. 따라서 실제 배포안은 lazy 방식으로 변경했다.

합성 grid 기준 단일 pass/seed 비교:
- full prebuild는 큰 frame에서 legacy보다 느려질 수 있음.
- lazy graph는 단일 seed에서 대체로 유사 수준이며, 동일 topology가 여러 pass/제도에서 재사용될수록 이득이 커짐.

## 합성 검증 결과
### 결과 동일성
- 12×12 기초단위구 grid
- `max_units=50 / 240`
- 여러 seed
- 도로/strong barrier 없음 및 있음
- legacy `_basic_unit_component()`와 R23 `_basic_unit_component_graph()` 결과 동일

### 중복 barrier 감소
5개 seed, 동일 component pass:
- legacy barrier 호출: **715회**
- R23 graph barrier 호출: **264회**
- unique neighbor edge: **264개**
- cache reuse: **451회**
- 동일 결과 유지

### synthetic timing 참고
단순 사각 grid/empty barrier 기준이며 실제 서울 도형 실측을 대체하지 않는다.
- 20×20, seed 8개: 약 **3.0x**
- 30×30, seed 8개: 약 **1.5x**
- 동일 topology를 단일 seed로 4 pass 반복: 약 **2.0x**

실제 효과는 서울 기초단위구 형상, road_union 복잡도, seed 수, strong barrier/outer closure 재계산 여부에 따라 달라지므로 배포 후 `rule_apply_ms`, `component_pass_ms`, `neighbor_topology_ms`, `component_barrier_evaluations`를 같이 확인해야 한다.

## 회귀검사
- `python -m py_compile app.py` PASS
- R15 targeted regression PASS
- R17 15/15 PASS
- R18 15/15 PASS
- R19 8/8 PASS
- R20 10/10 PASS
- R21 16/16 PASS
- R22 13/13 PASS
- R23 26/26 PASS

## R23 판정
- Rule 변경 없음
- UI 변경 없음
- 법정기준 변경 없음
- 가로구역 알고리즘의 중복 공간연산만 축소
- 실제 Render 동일 대상지 재측정 권장
