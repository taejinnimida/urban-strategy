// R40 도심공공주택복합 3유형 POPUP3 밀도/공공부담 표시전용 패치 V2
// 원칙: 입지판정·Fact·PASS/FAIL·후보순위는 변경하지 않는다.
// 공통 POPUP3 체계: 기본 용적률 -> 용도지역 변경 -> 계획가능 용적률 -> 특례상한 -> 공공부담·환수

function publicComplexLegalMaxFarForZone(z){
  const map={
    '제1종일반주거':200,
    '제2종일반주거(7층)':250,
    '제2종일반주거':250,
    '제3종일반주거':300,
    '준주거':500,
    '준공업':400,
    '근린상업':900,
    '일반상업':1300,
    '중심상업':1500,
    '유통상업':1100
  };
  return map[z]??null;
}

function publicComplexBaseFarForZone(z){
  // '기본 용적률'은 현 용도지역의 서울시 조례용적률을 뜻한다.
  // 법적상한용적률과 혼용하지 않는다.
  if(typeof ordinanceFarForZone==='function'){
    const v=ordinanceFarForZone(z);
    if(v!=null)return v;
  }
  const fallback={
    '제1종일반주거':150,
    '제2종일반주거(7층)':200,
    '제2종일반주거':200,
    '제3종일반주거':250,
    '준주거':400,
    '준공업':400,
    '근린상업':600,
    '일반상업':800,
    '중심상업':1000,
    '자연녹지':50
  };
  return fallback[z]??null;
}

function publicComplexZoneGroup(z){
  if(['제1종일반주거','제2종일반주거(7층)','제2종일반주거','제3종일반주거'].includes(z))return 'general_residential';
  if(z==='준주거')return 'semi_residential';
  if(z==='준공업')return 'semi_industrial';
  if(['근린상업','일반상업','중심상업','유통상업'].includes(z))return 'commercial';
  return 'other';
}

function publicComplexHistoricalSpeculationState(c){
  // 2025-09-07 당시 투기과열지구 여부는 현재 공통 Fact에 연결되어 있지 않다.
  // 임의 추정 금지. 향후 공식 이력 Fact 연결 시 아래 필드로 주입한다.
  const raw=c?.public_complex_non_speculation_20250907 ?? c?.non_speculation_20250907 ?? c?.nonSpeculation20250907 ?? null;
  if(raw===true||raw==='Y'||raw==='YES'||raw==='NON_SPECULATION')return 'ELIGIBLE';
  if(raw===false||raw==='N'||raw==='NO'||raw==='SPECULATION')return 'INELIGIBLE';
  return 'REVIEW';
}

function publicComplexTemporaryFarForZone(z){
  const legal=publicComplexLegalMaxFarForZone(z),group=publicComplexZoneGroup(z);
  if(legal==null)return null;
  if(z==='제1종일반주거'||z==='제2종일반주거(7층)'||z==='제2종일반주거')return Math.round(legal*1.2);
  if(z==='제3종일반주거'||group==='semi_residential')return Math.round(legal*1.4);
  return null;
}

function publicComplexZoningChangeProfile(typ,current,target){
  const explicitTarget=(target&&target!==current)?target:null;
  if(typ==='commercial'){
    return {
      mode:'principle_upzone',
      value:explicitTarget?`${current||'-'} → ${explicitTarget}`:'준주거지역 또는 상업지역으로 상향 원칙',
      note:explicitTarget?'선택한 계획 용도지역을 적용':'목표 용도지역을 임의 확정하지 않음 · 계획변수 선택 시 해당 변경경로 표시'
    };
  }
  if(typ==='industrial'){
    return {
      mode:'maintain',
      value:explicitTarget?`${current||'-'} → ${explicitTarget} · 별도 검토`:'준공업지역 유지 원칙',
      note:explicitTarget?'주거산업융합지구의 준공업 유지 원칙과 다른 변경은 자동 적용하지 않음':'용도지역 변경을 전제로 하지 않음'
    };
  }
  return {
    mode:'optional',
    value:explicitTarget?`${current||'-'} → ${explicitTarget}`:'현 용도지역 유지 또는 상향 가능',
    note:explicitTarget?'사용자가 선택한 계획 용도지역 적용':'유지/상향을 모두 열어두고 종상향을 자동 전제로 하지 않음'
  };
}

function publicComplexFarProfile(c,typOverride=null){
  const typ=typOverride||effectivePublicComplexType();
  const current=c?.zoning||'';
  const explicitTarget=(c?.targetZoning&&c.targetZoning!==current)?c.targetZoning:null;
  const finalZone=explicitTarget||current;
  const currentBaseFar=publicComplexBaseFarForZone(current);
  const currentGroup=publicComplexZoneGroup(current),finalGroup=publicComplexZoneGroup(finalZone);
  const legal=publicComplexLegalMaxFarForZone(finalZone);
  const zoningChange=publicComplexZoningChangeProfile(typ,current,explicitTarget);
  const historicalState=publicComplexHistoricalSpeculationState(c);
  const temporaryFar=(typ==='commercial'||typ==='housing')?publicComplexTemporaryFarForZone(finalZone):null;
  let baseFar=null,baseFarText='',nonResidentialRule='',parkingRule='';

  if(typ==='commercial'){
    if(finalGroup==='semi_residential'){
      baseFar=700;
      baseFarText='준주거지역 700%까지(법적상한 500%의 140%)';
    }else if(finalGroup==='commercial'&&legal!=null){
      baseFar=legal;
      baseFarText=`${finalZone} 법적상한 ${legal}%까지`;
    }else if(finalGroup==='general_residential'&&legal!=null){
      baseFar=Math.round(legal*1.2);
      baseFarText=`${finalZone} 법적상한 ${legal}%의 120% = ${baseFar}%까지`;
    }else{
      baseFarText='준주거 또는 상업지역 상향 후 해당 계획기준 적용';
    }
    nonResidentialRule='준주거지역은 비주거시설 비율을 확보하지 않을 수 있음 · 상업지역은 비주거시설 용적률 비율 5% 이상';
    parkingRule='관련 법령상 주차장 설치기준의 1/2 범위에서 완화 가능';
  }else if(typ==='industrial'){
    baseFar=400;
    baseFarText='준공업지역 법적상한용적률 400%까지';
    nonResidentialRule='기존 산업시설 비율 10% 미만인 주거지화 지역은 산업시설 비율을 확보하지 않을 수 있음';
    parkingRule='대중교통 연계대책 수립 + 교통영향평가심의위원회 심의 시 주차장 설치기준 완화 가능';
  }else{
    if(explicitTarget){
      if(finalGroup==='general_residential'&&legal!=null){
        baseFar=legal;
        baseFarText=`용도지역 상향 후 ${finalZone} 법적상한 ${legal}%까지`;
      }else if(finalGroup==='semi_residential'){
        baseFar=500;
        baseFarText='용도지역 상향 후 준주거지역 법적상한 500%까지';
      }else{
        baseFarText='변경 후 용도지역 계획기준 별도 확인';
      }
    }else if(currentGroup==='general_residential'){
      const curLegal=publicComplexLegalMaxFarForZone(current);
      baseFar=curLegal==null?null:Math.round(curLegal*1.2);
      baseFarText=curLegal==null?'현 용도지역 법적상한 확인':`기존 용도지역 유지 · 법적상한 ${curLegal}%의 120% = ${baseFar}%까지`;
    }else if(currentGroup==='semi_residential'){
      baseFar=500;
      baseFarText='준주거지역 법적상한 500%까지';
    }else{
      baseFarText='현 용도지역 또는 상향 후 용도지역 기준 확인';
    }
    nonResidentialRule='준주거지역에서는 비주거시설 비율을 확보하지 않을 수 있음';
    parkingRule='대중교통 연계대책 수립 + 교통영향평가심의위원회 심의 시 주차장 설치기준 완화 가능';
  }

  let temporaryText='';
  if(typ==='industrial'){
    temporaryText=''; // 공통 POPUP3 원칙: 해당하지 않는 특례상한 행은 숨긴다.
  }else if(temporaryFar!=null){
    if(historicalState==='ELIGIBLE')temporaryText=`2025-09-07 당시 비투기과열 확인 · 한시특례 최대 ${temporaryFar}% 적용 가능`;
    else if(historicalState==='INELIGIBLE')temporaryText='2025-09-07 당시 투기과열지구 · 제4항 제4호 한시특례 미적용';
    else temporaryText=`2025-09-07 당시 투기과열지구 여부 확인필요 · 비투기과열이면 최대 ${temporaryFar}% 시나리오 가능`;
  }else{
    temporaryText='최종 용도지역이 제1·2·3종일반주거 또는 준주거인 경우에만 제4항 제4호 한시특례 검토';
  }

  return {
    type:typ,currentZone:current,targetZone:explicitTarget,finalZone,
    currentBaseFar,zoningChange,
    baseFar,baseFarText,temporaryFar,temporaryText,historicalState,
    nonResidentialRule,parkingRule,
    publicFacilityRule:'무상귀속 공공시설 + 주택법상 기부채납 기반시설 + 공공임대주택·기숙사 등 공공필요시설의 면적을 합산하여 복합지구면적의 15% 이내로 계획하는 것을 원칙',
    parkGreenRule:'10만㎡ 미만은 공원·녹지 확보의무 면제 · 10만㎡ 이상은 세대당 2㎡ 또는 지구면적 5% 중 큰 면적',
    note:'15%는 종상향 공공기여율이 아니라 공공시설등 계획면적 합계 기준'
  };
}

function publicComplexDensity(c,typOverride=null){
  const p=publicComplexFarProfile(c,typOverride);
  return {
    zone:p.zoningChange.value,
    far:[p.baseFarText,p.temporaryText].filter(Boolean).join(' / '),
    contribution:`공공시설등 계획부담: 복합지구면적 15% 이내 원칙 · ${p.note}`,
    profile:p
  };
}

function publicComplexDensityPlanRowsForPopup(typ,store,vf,rr){
  const c=store?.common||{},p=publicComplexFarProfile(c,typ);
  const source=schemeSheetSourceFor('public_complex','용적률·공공성','PUBLIC_COMPLEX_GUIDE','공공주택 업무처리지침 제20조의7');
  const rows=[];

  // 공통 POPUP3 체계 1. 기본 용적률
  rows.push(popupPlanRow(
    '기본 용적률',
    p.currentZone?(p.currentBaseFar!=null?`${p.currentZone} 조례용적률 ${p.currentBaseFar}%`:`${p.currentZone} · 조례용적률 확인 필요`):'현 용도지역 확인 필요',
    '현 용도지역의 서울시 조례용적률 · 법적상한 및 사업특례와 구분',
    source
  ));

  // 2. 용도지역 변경
  rows.push(popupPlanRow(
    '용도지역 변경',
    p.zoningChange.value,
    p.zoningChange.note,
    source
  ));

  // 3. 계획가능 용적률
  rows.push(popupPlanRow(
    '계획가능 용적률',
    p.baseFarText||'확인 필요',
    typ==='commercial'
      ?'주거상업고밀: 일반주거 법적상한×120% · 준주거 700% · 상업지역 해당 법적상한'
      :typ==='industrial'
        ?'주거산업융합: 준공업 유지 원칙 · 법적상한 400% · 120/140 한시특례 미적용'
        :'주택공급활성화: 유지 일반주거 법적상한×120% · 상향 일반주거는 변경 후 법적상한 · 준주거 500%',
    source
  ));

  // 4. 특례상한 — 적용되는 유형에만 표시
  if(typ!=='industrial'){
    rows.push(popupPlanRow(
      '특례상한',
      p.temporaryText||'확인 필요',
      '2025-09-07 당시 비투기과열 주거지역(상향된 경우 포함): 1·2종 120%, 3종·준주거 140% · 역사적 지정여부 Fact 없으면 확인필요',
      source
    ));
  }

  // 5. 공공부담·환수
  rows.push(popupPlanRow(
    '공공부담·환수',
    '공공시설등 계획면적 합계: 복합지구면적의 15% 이내 원칙',
    `${p.publicFacilityRule} · ${p.note} · 도정법식 종상향 공공기여율로 환산하지 않음`,
    source
  ));

  // 사업별 추가 계획조건
  rows.push(popupPlanRow(
    typ==='industrial'?'산업시설 계획':'비주거시설 계획',
    p.nonResidentialRule,
    '용적률 단계와 별도 계획조건',
    source
  ));

  const area=Number(vf?.area?.m2||store?.common?.area||0),units=Number(vf?.units?.count||0);
  let parkValue='10만㎡ 미만 확보의무 면제';
  if(area>=100000){
    const five=area*0.05,byUnits=units>0?units*2:null;
    parkValue=byUnits!=null
      ?`세대당 2㎡(${Math.round(byUnits).toLocaleString('ko-KR')}㎡) vs 지구면적 5%(${Math.round(five).toLocaleString('ko-KR')}㎡) 중 큰 값`
      :`지구면적 5%(${Math.round(five).toLocaleString('ko-KR')}㎡)와 세대당 2㎡ 중 큰 값 · 계획세대수 확인필요`;
  }
  rows.push(popupPlanRow('공원·녹지',parkValue,p.parkGreenRule,source));
  rows.push(popupPlanRow('주차장',p.parkingRule,'세부유형별 완화조건을 충족하는 경우 적용',source));
  return rows.join('');
}
