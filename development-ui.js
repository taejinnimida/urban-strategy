/* R105: selection workflow. Seoul RULE functions remain unchanged. */
const devMode=document.documentElement.dataset.reviewMode;
let devSite=null,devSignature='',devBusy=false,devFacts=[];
const devRunOriginal=runSiteReview,devConfirmOriginal=confirmCurrentBoundary,devSummaryOriginal=renderBoundaryParcelSelectionSummary;
async function devApi(path,data,timeoutMs=0){
 const controller=timeoutMs?new AbortController():null,timer=controller?setTimeout(()=>controller.abort(),timeoutMs):null;
 try{const r=await fetch('/api/development/'+path,{method:data?'POST':'GET',headers:data?{'Content-Type':'application/json'}:{},body:data?JSON.stringify(data):undefined,cache:'no-store',...(controller?{signal:controller.signal}:{})});const v=await r.json();if(!r.ok)throw Error(typeof v.detail==='string'?v.detail:'요청 처리 실패');return v;}
 finally{if(timer)clearTimeout(timer);}
}
function devStatus(text){const el=document.getElementById('devStatus');if(el)el.textContent=text;}
function devRefresh(){if(devMode!=='select')return;const same=!!devSite&&devSignature===JSON.stringify(activeGeometry);const a=document.getElementById('siteReviewBtn'),b=document.getElementById('devNational');if(a){a.textContent='서울 도시정비 검토 ↗';a.disabled=!same||devBusy||devSite?.region!=='SEOUL';}if(b)b.disabled=!same||devBusy;if(a)a.title=!same?'먼저 구역계를 확정하세요.':devSite?.region==='SEOUL'?'서울 대상지 확인됨':devSite?.region==='OUTSIDE_OR_MIXED'?'서울 외 또는 경계 혼합 대상지입니다.':'서울 행정경계 확인이 필요합니다.';if(devSite&&!same)devStatus('구역계 변경: 다시 확정하세요. 이전 검토창의 대상지는 유지됩니다.');}
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
 fetchParcelAtMapPoint=async function(ll){
 const revision=boundaryAddressLookupRevision;
 const current=()=>revision===boundaryAddressLookupRevision&&boundaryInputMode==='address'&&parcelFindMode==='click';
 let v;
 try{v=await devApi('parcel-at-point',{lon:Number(ll.lng),lat:Number(ll.lat)},22000);}
 catch(e){v={status:'UNKNOWN',error_code:'REQUEST_FAILED'};}
 if(!current())throw Error('입력 상태가 변경되어 이전 클릭 결과를 적용하지 않았습니다.');
 if(v.status==='RESOLVED')return v.feature;
 if(v.status==='NOT_FOUND'||v.status==='AMBIGUOUS')throw Error(v.message||'필지 안쪽을 다시 클릭하세요.');
 // Respect provider rate limits. The CLIENT key is never a server-secret fallback.
 if(v.error_code==='RATE_LIMIT')throw Error('VWorld 호출 한도에 도달했습니다. 잠시 후 다시 클릭하세요.');
 if(!devClientReady())throw Error((devFailureLabels[v.error_code]||'서버 지적자료 연결 실패')+' · 브라우저용 연결도 사용할 수 없습니다. 관리자 연결상태를 확인하세요.');
 updateParcelClickStatus('서버 연결 실패 · VWorld 보조 경로에서 지적 조회 중…');
 let client;
 try{client=await devBrowserParcelAtPoint(ll);}
 catch(e){throw Error('VWorld 지적 연결 실패(서버·브라우저). 잠시 후 다시 시도하거나 관리자 연결상태를 확인하세요.');}
 if(!current())throw Error('입력 상태가 변경되어 이전 클릭 결과를 적용하지 않았습니다.');
 if(client.status==='RESOLVED')return client.feature;
 throw Error(client.message||'클릭점이 포함된 필지를 확인하지 못했습니다.');
 };
}
function devClientReady(){return typeof VWORLD_CLIENT_KEY==='string'&&!!VWORLD_CLIENT_KEY&&!VWORLD_CLIENT_KEY.startsWith('__');}
function devVWorldClientRequest(geometry,layer='LP_PA_CBND_BUBUN',page=1,size=100){
 if(!devLayerSpecs.some(spec=>spec[0]===layer)||!Number.isInteger(page)||page<1||page>10||![100,1000].includes(size))return Promise.reject(Error('CLIENT_REQUEST_NOT_ALLOWED'));
 if(!devClientReady())return Promise.reject(Error('CLIENT_CONNECTION_UNAVAILABLE'));
 const [a,b,c,d]=turf.bbox(turf.feature(geometry));
 // Fixed VWorld endpoint and allowlisted nationwide layers; never a general proxy.
 return new Promise((resolve,reject)=>{
 const callback='devParcel_'+Date.now()+'_'+Math.random().toString(36).slice(2),script=document.createElement('script');let settled=false;
 const cleanup=()=>{clearTimeout(timer);try{delete window[callback];}catch(e){window[callback]=undefined;}script.remove();};
 const finish=(error,value)=>{if(settled)return;settled=true;cleanup();error?reject(Error(error)):resolve(value);};
 const timer=setTimeout(()=>finish('CLIENT_TIMEOUT'),8000);
 window[callback]=payload=>finish(null,payload);script.onerror=()=>finish('CLIENT_NETWORK');script.referrerPolicy='strict-origin-when-cross-origin';
 const params=new URLSearchParams({key:VWORLD_CLIENT_KEY,domain:VWORLD_CLIENT_DOMAIN,service:'data',version:'2.0',request:'getfeature',format:'json',size:String(size),page:String(page),geometry:'true',attribute:'true',crs:'EPSG:4326',data:layer,geomfilter:`BOX(${a},${b},${c},${d})`,callback});
 script.src='https://api.vworld.kr/req/data?'+params;document.head.append(script);
 });
}
function devProviderFailure(rsp){
 const raw=String(rsp?.error?.code||'').toUpperCase();
 const code=['INCORRECT_KEY','INVALID_KEY','EXPIRED_KEY','UNAUTHORIZED_KEY'].includes(raw)?'CLIENT_AUTH':/LIMIT|QUOTA|TOO_MANY/.test(raw)?'RATE_LIMIT':/RANGE|PARAMETER|REQUEST/.test(raw)?'CLIENT_REQUEST':'CLIENT_PROVIDER_REJECTED';
 const error=Error(code);error.code=code;return error;
}
function devPickClientParcel(payload,ll){
 const rsp=payload?.response;
 if(rsp?.status==='NOT_FOUND')return {status:'NOT_FOUND',message:'클릭점의 지적을 찾지 못했습니다. 필지 안쪽을 다시 클릭하세요.'};
 if(rsp?.status!=='OK')throw devProviderFailure(rsp);
 const fs=rsp.result?.featureCollection?.features;
 if(!Array.isArray(fs)||fs.length>=100)throw Error('CLIENT_PROVIDER_FORMAT');
 const point=turf.point([Number(ll.lng),Number(ll.lat)]),hits=new Map();
 for(const f of fs){
 if(!f.geometry||!['Polygon','MultiPolygon'].includes(f.geometry.type))continue;
 let inside=false;try{inside=turf.booleanPointInPolygon(point,turf.feature(f.geometry));}catch(e){throw Error('CLIENT_GEOMETRY_FORMAT');}
 if(!inside)continue;
 const props={...(f.properties||{})},low=Object.fromEntries(Object.entries(props).map(([k,v])=>[k.toLowerCase(),v]));
 const pnu=[low.pnu,f.id].map(v=>String(v||'')).find(v=>/^\d{19}$/.test(v));if(!pnu)throw Error('CLIENT_PNU_FORMAT');
 props.pnu=pnu;if(low.jibun!=null)props.jibun=low.jibun;props._input_lookup_source='VWORLD_BROWSER';
 hits.set(pnu,{type:'Feature',id:pnu,geometry:f.geometry,properties:props});
 }
 if(hits.size>1)return {status:'AMBIGUOUS',message:'클릭점이 필지 경계에 걸렸습니다. 필지 안쪽을 클릭하세요.'};
 if(!hits.size)return {status:'NOT_FOUND',message:'클릭점이 포함된 필지를 확인하지 못했습니다.'};
 return {status:'RESOLVED',feature:[...hits.values()][0]};
}
async function devBrowserParcelAtPoint(ll){
 const geometry=turf.buffer(turf.point([Number(ll.lng),Number(ll.lat)]),8,{units:'meters'}).geometry;
 return devPickClientParcel(await devVWorldClientRequest(geometry),ll);
}
let devParcelSelectionLayer=null;
function devRenderParcelSelection(){
 if(!devParcelSelectionLayer)return;
 devParcelSelectionLayer.clearLayers();
 const features=[];
 for(const [key,f] of parcelFeatureMap){if(!selectedParcelPnus.has(String(key)))continue;const copy=cloneFeature(f);copy.properties={...copy.properties,pnu:String(key),_included:true};features.push(copy);}
 if(features.length)devParcelSelectionLayer.addData({type:'FeatureCollection',features});
 if(features.length&&!map.hasLayer(devParcelSelectionLayer))devParcelSelectionLayer.addTo(map);
 renderBoundaryParcelSelectionSummary();
}
function devInstallParcelSelection(){
 if(devMode!=='select'||devParcelSelectionLayer)return;
 const pane=map.getPane('devParcelSelection')||map.createPane('devParcelSelection');pane.style.zIndex='620';
 devParcelSelectionLayer=L.geoJSON(null,{pane:'devParcelSelection',bubblingMouseEvents:false,style:{color:'#fff',weight:3,fillColor:'#ef4444',fillOpacity:.25},onEachFeature:(f,layer)=>{
 const label=document.createElement('span');label.textContent=f.properties?.jibun||f.properties?.pnu||'선택필지';layer.bindTooltip(label);
 layer.on('click',e=>{L.DomEvent.stopPropagation(e);if(boundaryInputMode!=='address'||parcelFindMode!=='click')return;toggleParcelSelection(String(f.properties.pnu));});
 }}).addTo(map);
 // Keep site-input selection out of the hidden analysis-layer visibility switches.
 const originalSync=syncParcelLayerFromState;
 syncParcelLayerFromState=function(){if(devMode!=='select')return originalSync();devRenderParcelSelection();};
 const originalMode=setParcelFindMode;
 setParcelFindMode=function(mode){originalMode(mode);devRenderParcelSelection();if(mode==='click')updateParcelClickStatus('필지 선택 모드 켜짐 · 지도에서 필지 안쪽을 클릭하세요.');};
 devRenderParcelSelection();
}
const devLayerSpecs=[
 ['LP_PA_CBND_BUBUN','연속지적','parcel'],
 ...['도시지역','관리지역','농림지역','자연환경보전지역'].map((n,i)=>['LT_C_UQ11'+(i+1),n,'zoning']),
 ...['도로','교통시설','공간시설','유통·공급시설','공공·문화체육시설','방재시설','보건위생시설','환경기초시설','기타 기반시설'].map((n,i)=>['LT_C_UPISUQ15'+(i+1),n,'facility'])];
const devFailureLabels={SERVICE_NOT_READY:'자료 연결 설정 확인 필요',TEMPORARILY_PAUSED:'외부 연결 재시도 대기',RATE_LIMIT:'제공기관 호출 한도',PROVIDER_REJECTED:'제공기관 요청 거부',PROVIDER_UNREACHABLE:'제공기관 연결 실패',PROVIDER_FORMAT:'자료 응답 형식 확인 필요',REQUEST_FAILED:'자료 조회 실패',DEFERRED:'연결 복구 후 조회',CLIENT_AUTH:'VWorld 인증 또는 허용 도메인 확인 필요',CLIENT_REQUEST:'VWorld 조회 조건 확인 필요',CLIENT_PROVIDER_REJECTED:'VWorld 데이터 요청 거부',CLIENT_TIMEOUT:'브라우저 보조 조회 시간 초과',CLIENT_NETWORK:'브라우저 보조 연결 실패',CLIENT_CONNECTION_UNAVAILABLE:'브라우저용 연결을 사용할 수 없음',CLIENT_PROVIDER_FORMAT:'VWorld 응답 형식 확인 필요'};
function devOutcome(facts){
 const complete=facts.filter(v=>v.status==='COMPLETE').length,deferred=facts.filter(v=>v.status==='DEFERRED').length,failed=facts.length-complete-deferred;
 return {complete,failed,deferred,text:!facts.length?'자료 조회 대기':failed||deferred?`조회 완료 ${complete}개 · 조회 실패 ${failed}개 · 미조회 ${deferred}개`:'공간자료 조회 완료'};
}
function devFailureDescription(v){
 if(v.status==='DEFERRED')return '미조회 · '+(devFailureLabels[v.deferred_reason]||'재시도 필요');
 return '서버: '+(devFailureLabels[v.error_code]||'자료 조회 실패')+(v.client_error_code?' · 보조 연결: '+(devFailureLabels[v.client_error_code]||'조회 실패'):'');
}
const devRetryWait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
function devRenderFacts(){
 if(typeof devPlanningMap!=="undefined"&&devPlanningMap){try{devPlanningRefresh();}catch(e){const el=document.getElementById("devPlanningStatus");if(el)el.textContent="일부 도형 표시 오류 · 오른쪽 조회결과를 확인하세요.";}}
 for(const group of ['parcel','zoning','facility']){
 const host=document.getElementById('devFacts-'+group);if(!host)continue;host.replaceChildren();
 for(const [id,name,g] of devLayerSpecs.filter(v=>v[2]===group)){
 const v=devFacts.find(f=>f.layer===id),row=document.createElement('div');row.className='dev-fact-row';
 const title=document.createElement('strong');title.textContent=name;
 const badge=document.createElement('span');badge.className='dev-state '+(!v?'waiting':v.status==='COMPLETE'?'ok':'failed');badge.textContent=!v?'대기':v.status==='COMPLETE'?(v.features.length?'확보':'조회 0건'):v.status==='DEFERRED'?'미조회':'조회 실패';row.append(title,badge);
 const desc=document.createElement('p');desc.textContent=!v?'순차 조회 예정':v.status==='COMPLETE'?v.features.length+'건 · 대상지 중첩 '+v.overlap_area_m2.toLocaleString()+'㎡ · '+(v.route==='BROWSER'?'VWorld 보조 조회':'VWorld 서버 조회'):devFailureDescription(v);row.append(desc);
 if(v?.status==='COMPLETE'&&v.features.length){const names=[...new Set(v.features.map(f=>{const p=f.properties||{},lower=Object.fromEntries(Object.entries(p).map(([k,v])=>[k.toLowerCase(),v]));return lower.fclty_nm||lower.uname||lower.name||lower.jibun||lower.dgm_nm||'';}).filter(Boolean))];if(names.length){const line=document.createElement('p');line.className='dev-feature-names';line.textContent=names.slice(0,8).join(' · ')+(names.length>8?' 외 '+(names.length-8)+'건':'');row.append(line);}}
 host.append(row);
 }
 }
 const outcome=devOutcome(devFacts);const summary=document.getElementById('devConnectionSummary');if(summary){summary.textContent=devBusy?'자료 조회 중 · '+outcome.complete+'개 항목 확보 / '+devFacts.length+'개 항목 확인':outcome.text+' · '+outcome.complete+'/'+devLayerSpecs.length+' 항목 확보';summary.className=outcome.failed||outcome.deferred||outcome.complete===0?'dev-alert':'dev-alert dev-success';}
}
async function devBrowserLayer(layer,geometry){
 const spec=devLayerSpecs.find(row=>row[0]===layer);if(!spec)throw Error('CLIENT_REQUEST_NOT_ALLOWED');
 const target=turf.feature(geometry),features=[],seen=new Set();let merged=null;const started=Date.now();
 for(let page=1;page<=10;page++){
 if(Date.now()-started>22000)throw Error('CLIENT_TIMEOUT');
 const payload=await devVWorldClientRequest(geometry,layer,page,1000),rsp=payload?.response;
 if(rsp?.status==='NOT_FOUND')return {status:'COMPLETE',layer,name:spec[1],source:'VWorld',route:'BROWSER',features,overlap_area_m2:merged?turf.area(merged):0,queried_at:Date.now()/1000};
 if(rsp?.status!=='OK')throw devProviderFailure(rsp);
 const items=rsp.result?.featureCollection?.features;if(!Array.isArray(items))throw Error('CLIENT_PROVIDER_FORMAT');
 for(const f of items){
 if(!f.geometry)throw Error('CLIENT_PROVIDER_FORMAT');
 let intersects;try{intersects=turf.booleanIntersects(target,turf.feature(f.geometry));}catch(e){throw Error('CLIENT_PROVIDER_FORMAT');}if(!intersects)continue;
 const props=f.properties||{},id=String(f.id||props.pnu||props.unq_mnno||JSON.stringify(f.geometry));if(seen.has(id))continue;seen.add(id);features.push(f);
 if(['Polygon','MultiPolygon'].includes(f.geometry.type)){
  try{const clip=turf.intersect(target,turf.feature(f.geometry));if(clip)merged=merged?turf.union(merged,clip):clip;}catch(e){throw Error('CLIENT_PROVIDER_FORMAT');}
 }
 }
 if(items.length<1000)return {status:'COMPLETE',layer,name:spec[1],source:'VWorld',route:'BROWSER',features,overlap_area_m2:merged?turf.area(merged):0,queried_at:Date.now()/1000};
 }
 throw Error('CLIENT_PROVIDER_FORMAT');
}
async function devFetchNationalLayer(layer,geometry,state){
 const name=devLayerSpecs.find(s=>s[0]===layer)?.[1]||layer;
 let server={status:'UNKNOWN',error_code:'REQUEST_FAILED'};
 // Every layer gets its own server request. A bounded wait handles slot contention.
 for(let attempt=0;attempt<3;attempt++){
  try{server=await devApi('layer',{geometry,layer},42000);}catch(e){server={status:'UNKNOWN',error_code:'PROVIDER_UNREACHABLE'};}
  if(server.status==='COMPLETE')return server;
  if(server.error_code!=='TEMPORARILY_PAUSED'||attempt===2)break;
  const seconds=Number(server.retry_after_seconds);
  await devRetryWait(Math.max(1,Math.min(15,Number.isFinite(seconds)?seconds:1))*1000);
 }
 if(['RATE_LIMIT','SERVICE_NOT_READY','TEMPORARILY_PAUSED'].includes(server.error_code))return {...server,layer,name,source:'VWorld',features:[]};
 if(!devClientReady())return {...server,layer,name,source:'VWorld',features:[],client_error_code:'CLIENT_CONNECTION_UNAVAILABLE'};
 try{return await devBrowserLayer(layer,geometry);}
 catch(e){const code=e.code||e.message;const safe=Object.hasOwn(devFailureLabels,code)?code:'CLIENT_PROVIDER_FORMAT';return {status:'UNKNOWN',layer,name,source:'VWorld',features:[],error_code:server.error_code,client_error_code:safe};}
}
async function devLoadFacts(manual=false){
 if(devBusy||!devSite)return;devBusy=true;devFacts=[];devRenderFacts();
 const retry=document.getElementById('devFetch');if(retry)retry.disabled=true;
 if(window.devFactLayer)map.removeLayer(window.devFactLayer);window.devFactLayer=L.layerGroup().addTo(map);
 const state={};let terminalFailure=null;
 try{for(let i=0;i<devLayerSpecs.length;i++){
 const [layer,name]=devLayerSpecs[i];devStatus('공간자료 확인 중 · '+(i+1)+'/'+devLayerSpecs.length);
 if(terminalFailure){devFacts.push({layer,name,status:'DEFERRED',features:[],error_code:'DEFERRED',deferred_reason:terminalFailure});continue;}
 try{const v=await devFetchNationalLayer(layer,devSite.geometry,state);devFacts.push(v);
 if(v.status==='COMPLETE')L.geoJSON(v.features,{style:{color:layer==='LP_PA_CBND_BUBUN'?'#64748b':layer.startsWith('LT_C_UQ11')?'#087f8c':'#bd691c',weight:1,fillOpacity:.10},onEachFeature:(f,l)=>{const p=document.createElement('pre');p.textContent=JSON.stringify(f.properties,null,2);l.bindPopup(p);}}).addTo(window.devFactLayer);
 else if(['RATE_LIMIT','SERVICE_NOT_READY'].includes(v.error_code))terminalFailure=v.error_code;
 else if(v.client_error_code==='RATE_LIMIT')terminalFailure='RATE_LIMIT';
 }catch(e){devFacts.push({layer,name,status:'UNKNOWN',features:[],error_code:'REQUEST_FAILED'});}
 devRenderFacts();}
 }finally{devBusy=false;if(retry)retry.disabled=false;devRenderFacts();devStatus(devOutcome(devFacts).text+' · 사업 적합성은 별도 검토입니다.');}
}
// Display-only map. No extra data requests and no changes to FACT/RULE geometry.
let devPlanningMap=null,devPlanningGroups=null,devPlanningStamp='',devPlanningLabels=[];
function devPlanningSymbol(id,feature){
 const p=Object.fromEntries(Object.entries(feature.properties||{}).map(([k,v])=>[k.toLowerCase(),v]));
 const name=String(p.fclty_nm||p.uname||p.name||p.jibun||p.dgm_nm||'');
 if(id==='LP_PA_CBND_BUBUN')return {name:String(p.jibun||''),label:'지적경계',color:'#64748b',fillOpacity:0,weight:1,pane:'devParcel'};
 if(id.startsWith('LT_C_UQ11')){
  const palette=[[/주거/,'#eabf43'],[/상업/,'#ef7777'],[/공업/,'#a78bca'],[/녹지/,'#78bb83'],[/관리/,'#d4ae73'],[/농림/,'#9dc66a'],[/자연환경보전/,'#5da884']];
  const color=palette.find(([re])=>re.test(name))?.[1]||({'LT_C_UQ111':'#ddbd72','LT_C_UQ112':'#d4ae73','LT_C_UQ113':'#9dc66a','LT_C_UQ114':'#5da884'}[id])||'#b8c9d4';
  return {name,label:name||devLayerSpecs.find(s=>s[0]===id)?.[1]||'용도지역',color,fillOpacity:.38,weight:1.5,pane:'devZone'};
 }
 const colors=['#a77756','#7864ae','#419765','#3c9eae','#4878b2','#b36a9c','#b67a37','#568572','#758396'];
 const i=Number(id.slice(-1))-1;
 return {name,label:devLayerSpecs.find(s=>s[0]===id)?.[1]||'도시계획시설',color:colors[i]||'#758396',fillOpacity:.2,weight:2,dashArray:'5 3',pane:'devFacility'};
}
function devPlanningRefresh(){
 if(!devPlanningMap)return;
 const status=document.getElementById('devPlanningStatus');
 const complete=devFacts.filter(f=>f.status==='COMPLETE').length;
 status.textContent=`${devBusy?'자료 조회 중':'조회 종료'} · ${complete}/${devLayerSpecs.length}개 항목 조회 완료 · 대상지와 교차하는 확보 도형 표시. 빈 부분은 시설 없음이 아닙니다.`;
 const stamp=JSON.stringify(devFacts.map(f=>[f.layer,f.status,f.queried_at,f.features?.length]));
 if(stamp===devPlanningStamp)return;devPlanningStamp=stamp;
 for(const group of Object.values(devPlanningGroups))group.clearLayers();devPlanningLabels=[];
 const legend=document.getElementById('devPlanningLegend');legend.replaceChildren();
 const symbols=new Map([['확정 구역계',{color:'#dc2626'}]]);
 for(const fact of devFacts){
  if(fact.status!=='COMPLETE')continue;
  for(const f of fact.features||[]){
   if(!f.geometry)continue;
   const symbol=devPlanningSymbol(fact.layer,f);symbols.set(symbol.label,symbol);
   const group=fact.layer==='LP_PA_CBND_BUBUN'?'parcel':fact.layer.startsWith('LT_C_UQ11')?'zoning':'facility';
   L.geoJSON(f,{style:()=>symbol,onEachFeature:(_,layer)=>{
    const text=document.createElement('span');text.textContent=symbol.name||symbol.label;
    layer.bindTooltip(text,{direction:'center',className:'dev-plan-label',permanent:false});
    const popup=document.createElement('div');popup.textContent=`${symbol.label}${symbol.name&&symbol.name!==symbol.label?' · '+symbol.name:''} · VWorld`;
    layer.bindPopup(popup);
    if(symbol.name&&layer.getBounds)devPlanningLabels.push({layer,text,symbol,group});
   }}).addTo(devPlanningGroups[group]);
  }
 }
 for(const [name,symbol] of symbols){const item=document.createElement('span'),swatch=document.createElement('i');swatch.style.backgroundColor=symbol.color;item.append(swatch,document.createTextNode(name));legend.append(item);}
 devPlanningUpdateLabels();
}
function devPlanningUpdateLabels(){
 if(!devPlanningMap)return;
 const used=new Set();let shown=0;
 for(const entry of devPlanningLabels){
  const {layer,text,group}=entry;
  const point=layer.getBounds().getCenter(),pixel=devPlanningMap.latLngToContainerPoint(point);
  const key=Math.floor(pixel.x/110)+':'+Math.floor(pixel.y/28);
  const visible=devPlanningMap.hasLayer(devPlanningGroups[group])&&devPlanningMap.getBounds().contains(point)&&devPlanningMap.getZoom()>=16&&!used.has(key)&&shown<150;
  layer.unbindTooltip();layer.bindTooltip(text,{direction:'center',className:'dev-plan-label',permanent:visible});
  if(visible){used.add(key);shown++;}
 }
}
function devInstallPlanningMap(){
 if(devMode!=='development'||devPlanningMap)return;
 const target=document.querySelector('.service-target-box');if(!target)return;
 const box=document.createElement('section');box.id='devPlanningBox';
 box.innerHTML='<h3>도시계획정보도 <small>항공사진과 위치·확대 동기화</small></h3><div id="devPlanningMap" aria-label="도시계획정보도"></div><div id="devPlanningLegend"></div><p id="devPlanningStatus" role="status">자료 조회 대기</p><p class="dev-plan-source">자료: VWorld · 색상은 구분용 표현이며 법정 고시도면이 아닙니다. 확대하면 지번·명칭이 표시됩니다.</p>';
 target.append(box);
 devPlanningMap=L.map('devPlanningMap',{attributionControl:true,zoomControl:true,crs:map.options.crs,zoomSnap:map.options.zoomSnap,minZoom:map.getMinZoom(),maxZoom:map.getMaxZoom()});
 for(const [pane,z] of [['devZone',410],['devFacility',420],['devParcel',430],['devBoundary',440]])devPlanningMap.createPane(pane).style.zIndex=z;
 devPlanningGroups={zoning:L.layerGroup().addTo(devPlanningMap),facility:L.layerGroup().addTo(devPlanningMap),parcel:L.layerGroup().addTo(devPlanningMap)};
 L.geoJSON(devSite.geometry,{pane:'devBoundary',interactive:false,style:{color:'#dc2626',weight:3,fillOpacity:0}}).addTo(devPlanningMap);
 L.control.layers(null,{'용도지역':devPlanningGroups.zoning,'도시계획시설':devPlanningGroups.facility,'지적경계':devPlanningGroups.parcel},{collapsed:false}).addTo(devPlanningMap);
 L.control.scale({imperial:false}).addTo(devPlanningMap);
 const north=L.control({position:'topright'});north.onAdd=()=>{const el=L.DomUtil.create('div','dev-plan-north');el.textContent='↑ N';return el;};north.addTo(devPlanningMap);
 devPlanningMap.attributionControl.addAttribution('VWorld 공간정보');
 let syncing=false;
 const sync=(from,to)=>{if(syncing)return;syncing=true;try{to.setView(from.getCenter(),from.getZoom(),{animate:false});}finally{syncing=false;}};
 map.on('moveend',()=>sync(map,devPlanningMap));devPlanningMap.on('moveend',()=>{sync(devPlanningMap,map);devPlanningUpdateLabels();});
 devPlanningMap.on('overlayadd overlayremove',devPlanningUpdateLabels);
 const resize=()=>{const original=map.getContainer(),el=document.getElementById('devPlanningMap');el.style.height=original.clientHeight+'px';el.style.width=original.clientWidth+'px';devPlanningMap.invalidateSize({pan:false});sync(map,devPlanningMap);};
 window.addEventListener('resize',resize);
 if(typeof ResizeObserver!=='undefined'){const observer=new ResizeObserver(resize);observer.observe(map.getContainer());}
 setTimeout(resize,0);devPlanningRefresh();
}

function devInstallLayout(){
 const style=document.createElement('style');style.textContent=`
 #devReview{box-sizing:border-box;padding:22px;background:#fff;border:1px solid #dce4ee;border-radius:12px;color:#172b45;font:14px/1.6 Arial,sans-serif;min-width:0}
 #devReview h2{font-size:20px;margin:0 0 8px}#devReview h3{font-size:15px;margin:0}#devReview p{margin:6px 0}#devReview a{color:#175cd3}
 .dev-actions{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0}.dev-actions button{border:1px solid #cbd5e1;border-radius:7px;background:#f8fafc;padding:9px 12px;color:#17324f;font-size:12px;cursor:pointer}.dev-actions button:first-child{background:#087f8c;color:#fff;border-color:#087f8c}.dev-actions button:disabled{opacity:.45;cursor:wait}
 .dev-alert{padding:12px 14px;border:1px solid #fed7aa;background:#fff7ed;color:#9a3412;border-radius:8px;margin:12px 0}.dev-success{border-color:#99d5bd;background:#edf9f2;color:#166534}
 .dev-card{border:1px solid #e2e8f0;border-radius:9px;overflow:hidden;margin-top:12px}.dev-card>h3{padding:11px 14px;background:#f3f6fa}.dev-fact-row{padding:10px 14px;border-top:1px solid #edf0f4}.dev-fact-row strong{font-size:13px}.dev-fact-row p{font-size:12px;color:#526174}.dev-state{float:right;border-radius:20px;padding:1px 8px;font-size:11px;background:#f1f5f9;color:#64748b}.dev-state.ok{background:#e5f5ec;color:#166534}.dev-state.failed{background:#fff0e5;color:#9a3412}.dev-feature-names{color:#193c5a!important}.dev-scope{margin-top:16px;border-top:1px solid #e2e8f0;padding-top:12px;font-size:12px;color:#64748b}
 html[data-review-mode="development"] #serviceReviewPage{display:grid!important;grid-template-columns:minmax(0,1.15fr) minmax(360px,.85fr);gap:16px;padding:16px!important}
 html[data-review-mode="development"] #serviceReviewPage>.service-page-head,html[data-review-mode="development"] #devStatus{grid-column:1/-1;margin:0}
 html[data-review-mode="development"] #map{height:480px!important;min-height:320px!important}
 html[data-review-mode="development"] #ccMapInstruction,html[data-review-mode="development"] .boundary-review-bar,html[data-review-mode="development"] .map-foot .metric:last-child{display:none!important}
 html[data-review-mode="development"] .service-target-box{position:static;align-self:start;min-width:0}
 #devPlanningBox{margin-top:18px;border-top:1px solid #dce4ee;padding-top:12px}
 #devPlanningBox h3{margin:0 0 10px;font-size:15px}#devPlanningBox small{font-size:11px;font-weight:400;color:#64748b;margin-left:8px}
 #devPlanningMap{height:480px;max-width:100%;background:#f9fafb}
 #devPlanningLegend{display:flex;flex-wrap:wrap;gap:6px 14px;padding:10px 0;font-size:11px}#devPlanningLegend span{display:inline-flex;align-items:center;gap:5px}#devPlanningLegend i{display:inline-block;width:14px;height:10px;border:1px solid #777}
 #devPlanningStatus,.dev-plan-source{font-size:11px;line-height:1.6;margin:5px 0;color:#64748b}
 .dev-plan-label{font-size:11px;background:#ffffffd9;border:0;box-shadow:none;padding:1px 3px;color:#1e293b;pointer-events:none}
 .dev-plan-north{background:white;padding:5px 8px;font-weight:700;border:1px solid #ccc}
 @media(max-width:1000px){html[data-review-mode="development"] #serviceReviewPage{grid-template-columns:1fr}html[data-review-mode="development"] .service-target-box{position:static}html[data-review-mode="development"] #map{height:360px!important}}
 `;document.head.append(style);
}

function devExport(){const blob=new Blob([JSON.stringify({snapshot:devSite,facts:devFacts,rule_status:'NOT_IMPLEMENTED'},null,2)],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='development-review.json';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);}
async function devInit(){
 const head=document.querySelector('#serviceReviewPage .service-page-head');if(head){head.replaceChildren();const h=document.createElement('h2');h.textContent=devMode==='select'?'대상지 선정 · 전국 지도 [R105.4 개발본]':devMode==='seoul'?'서울 도시정비 검토 [R105.4 개발본]':'도시개발 검토 · 전국 공간현황 [R105.4 개발본]';head.append(h);}
 const status=document.createElement('p');status.id='devStatus';status.setAttribute('role','status');status.style.cssText='padding:12px;background:#edf6fa;color:#143852';head?.after(status);
 if(devMode!=='seoul'){const h=document.querySelector('.cc-header h1');if(h)h.textContent='도시정비·개발 검토 플랫폼';const sub=document.querySelector('.cc-header .sub');if(sub)sub.textContent='전국 대상지 선정 · R105.4 개발용';document.querySelectorAll('.main-map-layer-note').forEach(e=>e.textContent='전국 VWorld 용도지역·지적·시설 표시용 도면. 확대하여 확인하세요. 자료 미확보는 시설 없음이 아닙니다.');}
 if(devMode==='select'){
 devInstallParcelSelection();
 const b=document.createElement('button');b.id='devNational';b.textContent='도시개발 검토 ↗';b.disabled=true;b.onclick=()=>devOpen('development');document.getElementById('siteReviewBtn')?.after(b);
 const input=document.getElementById('boundaryAddressInput');if(input)input.placeholder='정식 시·도명부터 지번까지 · 여러 주소는 줄바꿈';devStatus('기존 지도·지번·SHP로 대상지를 선택한 뒤 구역계를 확정하세요. 검토는 각각 새 창에서 열립니다.');setInterval(devRefresh,600);devRefresh();return;
 }
 try{
 const token=new URLSearchParams(location.search).get('site');if(!token)throw Error('초기 화면에서 대상지를 확정한 뒤 검토창을 여세요.');devSite=await devApi('snapshots/'+encodeURIComponent(token));if(devMode==='seoul'&&devSite.region!=='SEOUL')throw Error('서울 포함 여부가 확인되지 않아 서울 사업검토를 실행할 수 없습니다.');
 activeGeometry=JSON.parse(JSON.stringify(devSite.geometry));boundaryReferenceGeometry=JSON.parse(JSON.stringify(activeGeometry));drawnItems.clearLayers();L.geoJSON(activeGeometry,{style:{color:'#dc2626',weight:4,fillOpacity:.04}}).eachLayer(l=>drawnItems.addLayer(l));setBoundaryReferenceMetrics();fitToActiveArea();
 devStatus(`확정 대상지 사본 · ${devSite.area_m2.toLocaleString()}㎡ · 원래 창을 수정해도 이 창의 대상지는 유지됩니다.`);
 if(devMode==='seoul'){await devRunOriginal();return;}
 devInstallLayout();
 devInstallPlanningMap();
 const section=document.createElement('section');section.id='devReview';section.innerHTML=`
 <h2>전국 도시개발 검토</h2><p>확정된 대상지의 기초 공간현황</p>
 <div class="dev-actions"><button id="devFetch">자료 다시 조회</button><button id="devExport">조회결과 저장</button></div>
 <div id="devConnectionSummary" role="status" class="dev-alert">자료 연결 확인 중</div>
 <section class="dev-card"><h3>지적 현황</h3><div id="devFacts-parcel"></div></section>
 <section class="dev-card"><h3>용도지역</h3><div id="devFacts-zoning"></div></section>
 <section class="dev-card"><h3>도시계획시설</h3><div id="devFacts-facility"></div></section>
 <div class="dev-scope"><b>검토 범위</b><p>현재는 공간현황 조회 단계입니다. 지방별 법령·조례에 따른 개발사업 자동 판정은 아직 제공하지 않습니다.</p><p>자료 미확보는 시설 없음 또는 사업 부적합을 뜻하지 않습니다.</p><p><a href="/api/development/admin/diagnostics" target="_blank" rel="noopener">관리자 연결상태 확인</a> · <a href="https://www.eum.go.kr" target="_blank" rel="noopener">토지이음</a></p></div>`;
 document.getElementById('serviceReviewPage').append(section);document.getElementById('devFetch').onclick=()=>devLoadFacts(true);document.getElementById('devExport').onclick=devExport;
 const targetTitle=document.querySelector('.service-target-box .service-block-title b');if(targetTitle)targetTitle.textContent='확정 대상지';const targetNote=document.querySelector('.service-target-box .service-block-title span');if(targetNote)targetNote.textContent='이 창에서는 구역계를 수정하지 않습니다.';
 setTimeout(()=>map.invalidateSize(false),0);await devLoadFacts();
 }catch(e){devSite=null;devStatus(e.message);}
}
