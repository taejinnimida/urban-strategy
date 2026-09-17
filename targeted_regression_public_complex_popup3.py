from pathlib import Path
import re, sys
s=Path('app.html').read_text(encoding='utf-8')
checks={
 'popup3_title':'3. 계획기준·용적률·공공부담' in s,
 'public_path':'사업경로 성격' in s and '공공주도 대안경로' in s,
 'facility_15':'공공시설 등 계획부담' in s and '복합지구면적 15% 이내 원칙' in s,
 'not_fixed_contribution':'정률 공공기여율이 아니라' in s,
 'rental_split':"const rentalPct=typ==='commercial'?15:10;" in s,
 'commercial_700':"준주거지역 700%까지" in s,
 'industrial_400':"준공업지역 법적상한용적률 400%까지" in s,
 'housing_500':"용도지역 상향 후 준주거지역 법적상한 500%까지" in s,
 'coverage_preserved':'건폐율 특례' in s,
 'park_preserved':"'공원·녹지'" in s,
 'parking_preserved':"'주차장'" in s,
 'flex_preserved':'계획기준 탄력적 적용' in s,
 'smallscale_preserved':'function renderSmallscaleSchemeDetailPopup()' in s,
}
for k,v in checks.items(): print(f'{k}: {"PASS" if v else "FAIL"}')
if not all(checks.values()): sys.exit(1)
print(f'TOTAL {sum(checks.values())}/{len(checks)} PASS')
