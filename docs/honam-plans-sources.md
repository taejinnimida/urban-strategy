# 호남권 상위·관련계획 1차 등록 (2026-11-14)

**브랜치:** `urban-plans` · **등록자료:** `plan_catalog_assets/honam-plans.json` · **추가:** 29건.

## 2026년 행정구역 개편 반영

2026-07-01 「전남광주통합특별시 설치를 위한 특별법」에 의해 종전 전라남도와 광주광역시는 전남광주통합특별시로 통합 출범했다. [행정안전부 보도자료](https://www.mois.go.kr/frt/bbs/type010/commonSelectBoardArticle.do?bbsId=BBSMSTR_000000000008&nttId=126845).

2026-10 기준 프로젝트 `regions.json`은 **전남광주통합특별시 `12`**, **전북특별자치도 `52`** 코드를 사용한다. 기존 `29`(광주)·`46`(전남) 코드로 등록하면 검증에 실패한다.

- 종전 광주 계획: `region=12` + `coverage_regions=[12210,12240,12270,12300,12330]` (옛 광주 5개 자치구).
- 종전 전남 도계획: `region=12` + 구 전라남도 22개 시·군 코드.
- 구 전남 시군계획: 통합특별시 행정구역으로 변경된 개별 시군 코드에 등록.
- 전북: 통합변경과 별개인 `52` 및 하위 14개 시군 코드.
- **주의:** 구 전남·광주 계획이 통합 후 언제까지 효력을 유지하는지, 통합특별시 차원의 신규 계획 수립이 있는지는 별도 법률·고시 검증이 필요하다. 통합특별시 전체 적용으로 임의 확대하지 않는다.

## 확인된 자료

- [제4차 전라북도 종합계획(2021~2040) 확정 공고](https://www.jeonbuk.go.kr/board/view.jeonbuk?boardId=BBS_0000129&dataSid=363932&menuCd=DOM_000000102002005000&orderBy=REGISTER_DATE%3ADESC%2CTMP_FIELD1%3ADESC&paging=ok&startPage=576)
- [제4차 전라남도 종합계획(2021~2040) 법정계획 원문](https://www.aurum.re.kr/Legal/LegalSub.aspx?pcode=A05)
- [2040 광주 기본계획 PDF](https://www.eum.go.kr/web/in/pd/pdBoardDet.jsp?bd_type=3&gbn=I&seq=291)
- [2040 순천 기본계획 PDF](https://www.eum.go.kr/web/in/pd/pdBoardDet.jsp?bd_type=3&gbn=I&seq=323)
- [2040 군산 기본계획 PDF](https://www.eum.go.kr/web/in/pd/pdBoardDet.jsp?bd_type=3&gbn=I&seq=303)
- [2030 전주시 도시·주거환경정비기본계획 2026 변경·정정고시](https://www.jeonju.go.kr/planweb/board/view.9is?boardUid=9be517a7914528ce019b041a879f395a&dataUid=eb90620063e444128325bf896ef2060d&page=7&tmpField14=)
- [2030 광주 도시·주거환경정비기본계획 고시(2022)](https://hreas.gwangju.go.kr/NOTICE/BoardView.aspx?bID=329&bType=2&pID=0)
- [제7차 전남권 관광개발계획 공고(2022)](https://touraz.kr/announcementList/pssrpView?bbsSeq=%EA%B8%B0%EA%B4%80%7C%EC%A0%84%EB%9D%BC%EB%82%A8%EB%8F%84%EC%B2%AD%7C202203%7C5054&tabMode=other)
- [광양 스마트도시계획 2025~2029](https://smartcity.go.kr/2025/07/07/%EA%B4%91%EC%96%91%EC%8B%9C-%EC%8A%A4%EB%A7%88%ED%8A%B8%EB%8F%84%EC%8B%9C%EA%B3%84%ED%9A%8D-2/)

## 미등록·검증 대기

- 전북 14개 시군 중 이번 공개본에서 다루지 못한 완주·진안·장수·임실·순창·고창·부안 등의 최신 기본계획(미수립으로 단정하지 않음).
- 통합특별시 구 전남 22개 시군 중 기본계획 미확인 지자체와 구 광주 자치구별 개별계획.
- 통합특별시 출범 이후 종전 광역계획의 법정 승계와 신규 광역 종합계획·경관·공업·재생·교통·주택 계획 수립 여부.
- 정비·도시재생전략 및 활성화·농촌재구조화·하천·연안·공원녹지·스마트도시 등 시군별 법정계획.
- 현재 공개된 계획자료는 등록되었다는 뜻이며 **관련계획 전수 구축이나 현행 법정 유효성 검증은 아니다.**

## 개발 경계

`urban-analysis` / `main` / Render 운영 소스를 직접 변경하지 않는다. 기존 `related_plans.py`의 파일 로드만 추가하며 기존 코드의 도시정비 판정 RULE은 변경하지 않는다.
