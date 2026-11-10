'use strict';
(() => {
  const $=id=>document.getElementById(id);
  let token=null, jobs=[];
  const output=(msg)=>{$('state').textContent=msg;};
  const API='/api/development/';
  async function api(path,method='GET',body) {
    const response=await fetch(API+path,{method,headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined,cache:'no-store'});
    const text=await response.text();let data;
    try{data=JSON.parse(text);}catch(_){throw Error('현황 서버 응답 오류');}
    if(!response.ok)throw Error(typeof data.detail==='string'?data.detail:'현황 서버 오류 '+response.status);
    return data;
  }
  const btn=(id,enabled)=>$(id).disabled=!enabled;
  const statusError=e=>output('자료 확인 실패: '+e.message);
  async function showReport() {
    if(!token)return;
    const data=await api('reports/'+encodeURIComponent(token));
    const items=Array.isArray(data.items)?data.items:[];
    $('summary').textContent='면적 '+Number(data.area_m2||0).toLocaleString('ko-KR')+'㎡ · 조회 항목 '+items.length+'개 · 미확보/참고자료는 별도 검증 필요';
    const table=document.createElement('table'),thead=document.createElement('thead'),tr=document.createElement('tr');
    for(const h of ['번호','현황 항목','자료 상태','출처','세부 내용']){
      const th=document.createElement('th');th.textContent=h;tr.append(th);
    }
    thead.append(tr);table.append(thead);
    const tbody=document.createElement('tbody');
    for(const item of items) {
      const row=document.createElement('tr');
      for(const value of [item.id,item.name,item.status,item.source,item.detail]) {
        const td=document.createElement('td');td.textContent=value==null?'미확보':String(value);row.append(td);
      }
      tbody.append(row);
    }
    table.append(tbody);$('results').replaceChildren(table);
    $('xlsx').href=API+'reports/'+encodeURIComponent(token)+'/xlsx';$('xlsx').hidden=false;
    output('현황조사 결과 조회 완료 · 현황값은 사업 적격성 판정이 아닙니다.');
  }
  $('create').addEventListener('click',async()=>{
    try{
      const geometry=JSON.parse($('geometry').value);
      const data=await api('snapshots','POST',{geometry});
      token=data.id;jobs=[];
      $('jobs').replaceChildren();$('results').replaceChildren();$('summary').textContent='';
      ['prepare','sources','refresh'].forEach(id=>btn(id,true));
      output('구역계 확정: '+Number(data.area_m2).toLocaleString('ko-KR')+'㎡ · 자료 유효시간 2시간');
    }catch(e){statusError(e);}
  });
  $('prepare').addEventListener('click',async()=>{
    try{output('현황조서 구성 중');await api('reports/'+encodeURIComponent(token)+'/prepare','POST');await showReport();}
    catch(e){statusError(e);}
  });
  $('refresh').addEventListener('click',async()=>{try{await showReport();}catch(e){statusError(e);}});
  $('sources').addEventListener('click',async()=>{
    try{
      if(!token)return;
      const response=await api('reports/'+encodeURIComponent(token)+'/source-jobs');
      jobs=response.jobs||[];const box=document.createElement('div');
      for(const job of jobs){
        const button=document.createElement('button');button.textContent=job.label||job.id;
        button.addEventListener('click',async()=>{
          button.disabled=true;output('조회 중: '+job.id);
          try{const result=await api('reports/'+encodeURIComponent(token)+'/source-jobs/'+encodeURIComponent(job.id),'POST');
            $('jobOutput').hidden=false;$('jobOutput').textContent=JSON.stringify({job:job.id,...result},null,2);output('자료 상태: '+(result.status||'UNKNOWN'));}
          catch(e){statusError(e);}finally{button.disabled=false;}
        });
        box.append(button);
      }
      $('jobs').replaceChildren(box);output('자료 항목 '+jobs.length+'개. 개별 항목 조회 결과는 원자료 확보 여부를 명시합니다.');
    }catch(e){statusError(e);}
  });
})();