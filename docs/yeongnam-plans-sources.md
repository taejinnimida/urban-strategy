# 영남권 상위 및 관련계획 1차 등록·출처 정리

- 기준일: 2026-11-14
- 데이터: `plan_catalog_assets/yeongnam-plans.json`
- 브랜치: `urban-plans`
- 신규 등록: **26개 계획, 자료 링크 28개**
- 대상: **부산·대구·울산·경상북도·경상남도**. 시·군별 분야계획의 전수 등록은 아니다.

## 분류별 주요 등록 자료

| 권역 | 대표 계획 | 상태 |
| --- | --- | --- |
| 부산 | 2040 도시기본계획, 2020 부산권 광역도시계획 | 기본계획/이전 광역계획 |
| 부산 | 2030 도시·주거환경정비기본계획 2026 변경 | 변경 |
| 부산 | 2035 노후계획도시정비기본계획(화명·금곡/해운대/다대) | 2개 확정, 다대 공람안 |
| 부산 | 제7차 부산권 관광개발계획 | 확정계획 공개 |
| 대구 | 2040 도시기본계획, 2030 정비기본계획 2026 변경 | 확정/변경 |
| 울산 | 2035 도시기본계획, 2040 도시기본계획(안), 2030 도시관리계획 재정비 | 확정/공청회안/변경 |
| 울산 | 제7·8차 관광개발계획 | 7차 공개, 8차 수립 중 |
| 경북 | 도 종합계획, 구미·영주·영천·상주 도시기본계획, 제7차 관광개발계획 | 공개본 |
| 경남 | 제4차 도 종합계획, 창원·밀양·양산 도시기본계획, 창원 경관계획, 제7차 관광개발계획 | 공개본/변경 |

## 공식 자료(예시)

- [토지이음 2040 부산 도시기본계획](https://www.eum.go.kr/web/in/pd/pdBoardDet.jsp?bd_type=3&gbn=I&seq=245&subType=C)
- [토지이음 2040 대구 도시기본계획](https://www.eum.go.kr/web/in/pd/pdBoardDet.jsp?bd_type=3&gbn=I&seq=330&subType=C)
- [토지이음 2040 영천 도시기본계획](https://www.eum.go.kr/web/in/pd/pdBoardDet.jsp?bd_type=3&gbn=I&seq=328&subType=C)
- [토지이음 2040 상주 도시기본계획](https://www.eum.go.kr/web/in/pd/pdBoardDet.jsp?bd_type=3&gbn=I&seq=331&subType=C)
- [토지이음 2040 밀양 도시기본계획](https://www.eum.go.kr/web/in/pd/pdBoardDet.jsp?bd_type=3&gbn=I&seq=287&subType=C)
- [토지이음 2040 양산 도시기본계획](https://www.eum.go.kr/web/in/pd/pdBoardDet.jsp?bd_type=3&gbn=I&seq=296&subType=C)
- [토지이음 2030 영주 도시기본계획](https://www.eum.go.kr/web/in/pd/pdBoardDet.jsp?bd_type=3&gbn=I&seq=212&subType=C)
- [경북 도종합계획 원문 · 경북도청](https://www.gb.go.kr/Main/page.do?BD_CODE=bbs_gongji&B_LEVEL=0&B_NUM=194734801&B_STEP=194734800&Start=0&V_NUM=20676&bdName=&cmd=2&dept_code=&dept_name=&key=0&mnu_uid=6786&p1=0&p2=0&tbbscode1=bbs_gongji&word=)
- [경남 도종합계획 원문 · 경남기록원](https://archives.gyeongnam.go.kr/00090/00092/00095.web?amode=view&cpage=94&idx=19491)
- [부산 2030 도시·주거환경정비기본계획 변경 고시](https://www.busan.go.kr/nbgosi/view?curPage=79&gosiGbn=N&sno=78657)
- [대구 2030 도시·주거환경정비기본계획 변경](https://www.daegu.go.kr/build/index.do?menu_id=00001812)
- [울산 도시기본계획 공식 페이지](https://www.ulsan.go.kr/u/traffic/contents.ulsan?mId=001007003000000000)

## 행정구역·법정 적용 주의

1. 2023-07-01 편입된 **대구광역시 군위군(27720)** 때문에 종전 경북 도종합계획·광역계획의 수립 당시 권역과 현재 행정구역이 완전히 같지 않을 수 있다. 현재 GIS 행정구역 조회가 법정 계획 적용경계 증명은 아니다.
2. 부산권 광역도시계획은 **부산·김해·양산**을 공동 권역으로 조회하지만, 각 시군 전체의 개별 필지에 특정 용도·규제를 직접 자동 적용하지 않는다.
3. 계획원문 **게시일**과 법정 승인·고시일을 구분한다. 공청회·공람을 진행한 2040 울산 도시기본계획 및 다대지구 노후계획도시기본계획은 최종 승인 여부를 확인하기 전까지 계획안으로 둔다.
4. 경북·경남 40개 시·군(경북 22, 경남 18)과 부산·대구·울산 각 구·군의 분야별 정비·재생·산업·교통·환경·농촌 관련계획은 별도 조사해야 한다.

## 후속 수집 우선순위

- 경북·경남의 누락 시·군 도시·군기본계획 공개본 확보, 이후 분야별 정비·도시재생전략·활성화계획.
- 부산·대구·울산의 **공업지역기본계획**, 도시재생전략계획, 경관·공원녹지기본계획, 건축자산 진흥 시행계획.
- 경북·경남의 농촌공간 재구조화·재생 기본계획, 경관·관광지 조성계획, 산업단지 관리기본계획, 환경·교통계획.
- 각 계획의 실제 고시문·승인 원문, 최근 변경, 계획권 지형도면, 2026년 법정 효력.

이번 등록은 GitHub `urban-plans`의 계획자료를 확장한 것이며 `urban-analysis`, `main`, Render 운영 배포는 수정하지 않았다. **코드 테스트는 추가하였으나 실제 Python/FastAPI 통합 실행은 아직 확인하지 않았다.**
