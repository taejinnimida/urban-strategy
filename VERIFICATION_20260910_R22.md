# VERIFICATION_20260910_R22

## 기준본
- `urban-strategy-v2.5.0-20260910-frontage-legal-r21-PATCH.zip`
- 앱 내부 버전 `v2.5.0` 유지.
- R21 접도율 법령정합, R20 가로구역 면적 선필터, R19 도로 100m 범위 모두 보존.

## R22 목적
가로구역 flood-fill에서 고정된 `road_union` / `strong_union`에 대해 반복 실행되는 Shapely predicate(`intersects`)를 prepared geometry로 가속한다.
Rule, 임계값, 공간판정식과 결과 geometry는 변경하지 않는다.

## 코드 변경
### app.py
1. `from shapely.prepared import prep` 추가.
2. `_shared_edge_barrier()`
   - `road_prepared`, `strong_prepared` 선택 인자 추가.
   - `intersects`만 prepared geometry로 실행.
   - `corridor.intersection(...).area`는 원본 geometry를 그대로 사용.
   - prepared predicate 실패 시 기존 raw predicate로 fallback.
3. `_basic_unit_component()`
   - prepared 객체를 전달받아 모든 공유경계 판정에서 재사용.
4. `_street_block_apply_rule()`의 `_components_for()`
   - component pass 시작 시 road/strong union을 한 번만 `prep()`.
   - seed별 flood-fill에서 동일 prepared 객체를 공유.
5. 성능진단 metadata 추가
   - `prepared_geometry`
   - `component_pass_count`
   - `component_seed_runs`
   - `component_pass_ms`
   - `prepared_build_ms`

## 정확성 원칙
- prepared geometry는 불리언 predicate만 가속한다.
- 실제 겹침면적 및 기존 `0.22 / 0.12` barrier 비율 임계값은 변경하지 않는다.
- 따라서 R22는 성능 패치이며 Rule 패치가 아니다.

## 로컬 합성 벤치마크
Shapely 2.1.2, 약 734KB WKB 규모의 조밀한 합성 도로 union + 1,200개 corridor.

- raw `intersects`: 평균 약 **43.9ms**
- prepared `intersects`: 평균 약 **6.4ms**
- predicate 구간: 약 **6.8x**
- hit count 동일: 825건

추가로 `_shared_edge_barrier()` 전체를 hit 비율이 높은 합성조건에서 비교한 결과는 약 1.0x 수준이었다. 이유는 predicate 이후 실행되는 `intersection().area`가 지배하기 때문이다. 따라서 실제 배포대상지에서 `component_pass_ms`를 측정하고, 여전히 큰 경우 R23에서 **공통 neighbor-pair + barrier별 adjacency graph**로 동일 경계의 반복 intersection 자체를 제거하는 것이 다음 단계다.

## 검증
- `python -m py_compile app.py` PASS
- R15 targeted regression PASS
- R17 15/15 PASS
- R18 15/15 PASS
- R19 8/8 PASS
- R20 10/10 PASS
- R21 16/16 PASS
- R22 13/13 PASS

R22 신규검사는 prepared/raw `_shared_edge_barrier()` 결과가 road barrier / strong barrier / mergeable / short-touch 4개 합성 케이스에서 완전히 동일함을 확인한다.

## 배포 후 확인할 로그/값
동일 대상지에서 R21 대비 다음 metadata를 비교한다.
- `common_prep_ms`
- `rule_apply_ms`
- `component_pass_count`
- `component_seed_runs`
- `component_pass_ms`
- `prepared_build_ms`

특히 `prepared_build_ms`는 작지만 `component_pass_ms`가 계속 크면 prepared 추가조정이 아니라 인접그래프 리팩터링 대상이다.
