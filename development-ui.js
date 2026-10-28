/* R105: selection workflow. Seoul RULE functions remain unchanged. */
const devMode=document.documentElement.dataset.reviewMode;
let devSite=null,devSignature='',devBusy=false,devFacts=[];
const devRunOriginal=runSiteReview,devConfirmOriginal=confirmCurrentBoundary,devSummaryOriginal=renderBoundaryParcelSelectionSummary;
async function devApi(path,data){const r=await fetch('/api/development/'+path,{method:data?'POST':'GET',headers:data?{'Content-Type':'application/json'}:{},body:data?JSON.stringify(data):undefined,cache:'no-store'});const v=await r.json();if(!r.ok)throw Error(typeof v.detail==='string'?v.detail:'요청 처리 실패');return v;}
function devStatus(text){const el=document.getElementById('devStatus');if(el)el.textContent=text;}
function devRefresh(){if(devMode!=='select')return;const same=!!devSite&&devSignature===JSON.stringify(activeGeometry);const a=document.getElementById('siteReviewBtn'),b=document.getElementById('devNational');if(a){a.textContent='서울 도시정비 검토 ↗';a.disabled=!same||devBusy||devSite?.region!=='SEOUL';}if(b)b.disabled=!same||devBusy;if(devSite&&!same)devStatus('구역계 변경: 다시 확정하세요. 이전 검토창의 대상지는 유지됩니다.');}
renderBoundaryParcelSelectionSummary=function(){devSummaryOriginal();devRefresh();};
confirmCurrentBoundary=async function(){
 if(devMode!=='select'||devBusy)return;devBusy=true;devRefresh();
 try{
 if(selectedParcelPnus.size){if(!await applySelectedParcelsAsBoundary()){devStatus('구역계 확정이 취소되었습니다.');return;}}else await devConfirmOriginal();
 if(!activeGeometry)return;const geometry=JSON.parse(JSON.stringify(activeGeometry)),signature=JSON.stringify(geometry);devStatus('구역계 확정 및 서울 행정경계 확인 중…');
 const v=await devApi('snapshots',{geometry});if(signature!==JSON.stringify(activeGeometry)){devStatus('확정 중 구역계가 변경되었습니다. 다시 확정하세요.');return;}
 devSite=v;devSignature=signature;devStatus(`확정 면적 ${v.area_m2.toLocaleString()}㎡ · ${v.region==='SEOUL'?'서울 대상지':v.region==='OUTSIDE_OR_MIXED'?'서울 외 또는 경계 혼합':'행정경계 미확보: 서울 검토 잠김'} · 검토창 유효시간 2시간`);
 }catch(e){devStatus(e.message);}finally{devBusy=false;devRefresh();}
};
function devOpen(mode){if(!devSite||devSignature!==JSON.stringify(activeGeometry)||devBusy)return;if(mode==='seoul'&&devSite.region!=='SEOUL')return;const w=window.open('/?review='+mode+'&site='+encodeURIComponent(devSite.id),'_blank');if(w)w.opener=null;else devStatus('팝업이 차단되었습니다. 새 창 열기를 허용하세요.');}
runSiteReview=async function(){if(devMode==='select')return devOpen('seoul');if(devMode==='seoul'&&devSite?.region==='SEOUL')return devRunOriginal();};
if(devMode!=='seoul'){
 measureAndSync=async function(){if(activeGeometry){boundaryReferenceGeometry=JSON.parse(JSON.stringify(activeGeometry));setBoundaryReferenceMetrics();}devRefresh();};
 runSchemeChecksWhenCoreReady=()=>{};updateCandidateSchemes=()=>{};refreshCompactMiniMaps=()=>{};r101Schedule=()=>{};loadBuiltInStationDataset=()=>{};loadBuiltInCenterDataset=()=>{};
 const oldParse=parseBoundaryAddressEntries;
 parseBoundaryAddressEntries=function(text){const rows=[],invalid=[];for(const line of String(text||'').split(/[\n;]/).map(s=>s.trim()).filter(Boolean)){const clean=line.replace(/,/g,' ').replace(/\s+/g,' ');if(/^(서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충청|충북|충남|전라|전북|전남|경상|경북|경남|제주)\S*\s+.+\s+(?:산\s*)?\d+(?:-\d+)?$/.test(clean))rows.push({query:clean,label:clean});else{const v=oldParse(line);rows.push(...v.rows.map(r=>({...r,query:`서울특별시 ${r.gu} ${r.dong} ${r.jibun}`})));invalid.push(...v.invalid);}}return {rows,invalid};};
 resolveSeoulParcelAddress=async function(row){const v=await devApi('address',{address:row.query});if(v.status!=='resolved')throw Error(v.message||'필지 미확보');return v.feature;};
 fetchParcelAtMapPoint=async function(ll){const pt=turf.point([ll.lng,ll.lat]);const v=await devApi('layer',{geometry:turf.buffer(pt,2,{units:'meters'}).geometry,layer:'LP_PA_CBND_BUBUN'});if(v.status!=='COMPLETE')throw Error(v.message);return v.features.find(f=>turf.booleanPointInPolygon(pt,f))||null;};
}
async function devLoadFacts(){
 if(devBusy||!devSite)return;devBusy=true;devFacts=[];
 const host=document.getElementById('devFacts');host.replaceChildren();
 if(window.devFactLayer)map.removeLayer(window.devFactLayer);window.devFactLayer=L.layerGroup().addTo(map);
 const layers=['LP_PA_CBND_BUBUN',...Array.from({length:4},(_,i)=>'LT_C_UQ11'+(i+1)),...Array.from({length:9},(_,i)=>'LT_C_UPISUQ15'+(i+1))];
 for(const layer of layers){const row=document.createElement('p');row.textContent=layer+' 조회 중…';host.append(row);try{
 const v=await devApi('layer',{geometry:devSite.geometry,layer});devFacts.push(v);row.textContent=`${v.name} · ${v.status==='COMPLETE'?v.features.length+'건 / 중첩 '+v.overlap_area_m2.toLocaleString()+'㎡':'미확보'} · ${v.source}`;
 const detail=document.createElement('details'),summary=document.createElement('summary'),pre=document.createElement('pre');summary.textContent='시설명·속성·조회정보';pre.style.cssText='white-space:pre-wrap;max-height:200px;overflow:auto';pre.textContent=JSON.stringify({layer:v.layer,queried_at:v.queried_at,features:v.features.map(f=>f.properties)},null,2);detail.append(summary,pre);host.append(detail);
 if(v.status==='COMPLETE')L.geoJSON(v.features,{style:{color:layer==='LP_PA_CBND_BUBUN'?'#64748b':'#087f8c',weight:1,fillOpacity:.08},onEachFeature:(f,l)=>{const p=document.createElement('pre');p.textContent=JSON.stringify(f.properties,null,2);l.bindPopup(p);}}).addTo(window.devFactLayer);
 }catch(e){row.textContent=layer+' 미확보: '+e.message;devFacts.push({layer,status:'UNKNOWN'});}}
 devBusy=false;devStatus('조회 완료. 미확보는 시설 없음이나 사업 부적합을 뜻하지 않습니다.');
}
function devExport(){const blob=new Blob([JSON.stringify({snapshot:devSite,facts:devFacts,rule_status:'NOT_IMPLEMENTED'},null,2)],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='development-review.json';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);}
async function devInit(){
 const head=document.querySelector('#serviceReviewPage .service-page-head');if(head){head.replaceChildren();const h=document.createElement('h2');h.textContent=devMode==='select'?'대상지 선정 · 전국 지도 [R105 개발본]':devMode==='seoul'?'서울 도시정비 검토 [R105 개발본]':'도시개발 검토 · 전국 공간현황 [R105 개발본]';head.append(h);}
 const status=document.createElement('p');status.id='devStatus';status.setAttribute('role','status');status.style.cssText='padding:12px;background:#edf6fa;color:#143852';head?.after(status);
 if(devMode!=='seoul'){const h=document.querySelector('.cc-header h1');if(h)h.textContent='도시정비·개발 검토 플랫폼';const sub=document.querySelector('.cc-header .sub');if(sub)sub.textContent='전국 대상지 선정 · R105 개발용';document.querySelectorAll('.main-map-layer-note').forEach(e=>e.textContent='전국 VWorld 용도지역·지적·시설 표시용 도면. 확대하여 확인하세요. 자료 미확보는 시설 없음이 아닙니다.');}
 if(devMode==='select'){
 const b=document.createElement('button');b.id='devNational';b.textContent='도시개발 검토 ↗';b.disabled=true;b.onclick=()=>devOpen('development');document.getElementById('siteReviewBtn')?.after(b);
 const input=document.getElementById('boundaryAddressInput');if(input)input.placeholder='정식 시·도명부터 지번까지 · 여러 주소는 줄바꿈';devStatus('기존 지도·지번·SHP로 대상지를 선택한 뒤 구역계를 확정하세요. 검토는 각각 새 창에서 열립니다.');setInterval(devRefresh,600);devRefresh();return;
 }
 try{
 const token=new URLSearchParams(location.search).get('site');if(!token)throw Error('초기 화면에서 대상지를 확정한 뒤 검토창을 여세요.');devSite=await devApi('snapshots/'+encodeURIComponent(token));if(devMode==='seoul'&&devSite.region!=='SEOUL')throw Error('서울 포함 여부가 확인되지 않아 서울 사업검토를 실행할 수 없습니다.');
 activeGeometry=JSON.parse(JSON.stringify(devSite.geometry));boundaryReferenceGeometry=JSON.parse(JSON.stringify(activeGeometry));drawnItems.clearLayers();L.geoJSON(activeGeometry,{style:{color:'#dc2626',weight:4,fillOpacity:.04}}).eachLayer(l=>drawnItems.addLayer(l));setBoundaryReferenceMetrics();fitToActiveArea();
 devStatus(`확정 대상지 사본 · ${devSite.area_m2.toLocaleString()}㎡ · 원래 창을 수정해도 이 창의 대상지는 유지됩니다.`);
 if(devMode==='seoul'){await devRunOriginal();return;}
 const section=document.createElement('section');section.id='devReview';section.style.cssText='padding:24px;border:1px solid #cad5df;background:white;margin:16px 0';section.innerHTML='<h2>도시개발 검토</h2><p>지적·용도지역·도시계획시설 중첩자료입니다. 비도시지역 여부나 사업 적합성을 이 자료만으로 확정하지 않습니다.</p><button id="devFetch">공간자료 다시 조회</button> <button id="devExport">조회결과 JSON 저장</button><div id="devFacts"></div><h3>인허가 검토기준 연결 상태</h3><p>도시개발사업·지구단위계획·개발행위허가·농지/산지전용·환경영향평가: 지방별 법령·조례 자동 판정은 미구현입니다.</p><p><a href="https://www.law.go.kr" target="_blank" rel="noopener">국가법령정보센터</a> · <a href="https://www.eum.go.kr" target="_blank" rel="noopener">토지이음</a></p>';document.getElementById('serviceReviewPage').append(section);document.getElementById('devFetch').onclick=devLoadFacts;document.getElementById('devExport').onclick=devExport;await devLoadFacts();
 }catch(e){devSite=null;devStatus(e.message);}
}
