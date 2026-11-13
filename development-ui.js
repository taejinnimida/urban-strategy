/* R105: selection workflow. Seoul RULE functions remain unchanged. */
const devMode=document.documentElement.dataset.reviewMode;
let devSite=null,devSignature='',devBusy=false,devFacts=[];
const devRunOriginal=runSiteReview,devConfirmOriginal=confirmCurrentBoundary,devSummaryOriginal=renderBoundaryParcelSelectionSummary;
async function devApi(path,data,timeoutMs=0){
 const controller=timeoutMs?new AbortController():null,timer=controller?setTimeout(()=>controller.abort(),timeoutMs):null;
 try{
  const r=await fetch('/api/development/'+path,{method:data?'POST':'GET',headers:data?{'Content-Type':'application/json'}:{},body:data?JSON.stringify(data):undefined,cache:'no-store',...(controller?{signal:controller.signal}:{})});
  let v;try{v=await r.json();}catch(parseError){if(parseError.name==='AbortError')throw parseError;const e=Error('플랫폼 응답 형식 확인 필요');e.code=r.ok?'PLATFORM_FORMAT':'PLATFORM_HTTP_ERROR';e.httpStatus=r.status;throw e;}
  if(!r.ok){const e=Error(typeof v.detail==='string'?v.detail:'플랫폼 요청 처리 실패');e.code=r.status===429?'PLATFORM_BUSY':'PLATFORM_HTTP_ERROR';e.httpStatus=r.status;throw e;}
  return v;
 }catch(e){if(!e.code)e.code=e.name==='AbortError'?'PLATFORM_TIMEOUT':'PLATFORM_CONNECTION';throw e;}
 finally{if(timer)clearTimeout(timer);}
}
function devStatus(text){const el=document.getElementById('devStatus');if(el){el.textContent=text;el.hidden=!text;}}
function devEnsureBaseMap(){
 if(devMode!=='select'||typeof map==='undefined'||!map)return;
 const hasFallback=[...Object.values(map._layers||{})].some(layer=>layer?._devBaseFallback);
 if(!hasFallback){
  const fallback=L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{
   maxZoom:19,attribution:'&copy; OpenStreetMap contributors',crossOrigin:true
  });
  fallback._devBaseFallback=true;
  fallback.addTo(map);
  if(typeof fallback.setZIndex==='function')fallback.setZIndex(-100);
 }
 const visibleTiles=[];
 map.eachLayer(layer=>{if(layer instanceof L.TileLayer&&map.hasLayer(layer)&&!layer._devBaseFallback)visibleTiles.push(layer);});
 if(!visibleTiles.length&&devClientReady()){
  const url='https://api.vworld.kr/req/wmts/1.0.0/'+encodeURIComponent(VWORLD_CLIENT_KEY)+'/Base/{z}/{y}/{x}.png';
  const vworld=L.tileLayer(url,{maxZoom:19,attribution:'VWorld'});
  let loaded=false,errors=0;
  vworld.on('tileload',()=>{loaded=true;});
  vworld.on('tileerror',()=>{errors++;if(errors>=4&&!loaded)devStatus('VWorld 기본지도 인증 또는 허용 도메인을 확인하세요. 임시 배경지도를 표시합니다.');});
  vworld.addTo(map);
 }
 setTimeout(()=>{try{map.invalidateSize(false);}catch(e){}},0);
}

function devRefresh(){if(devMode!=='select')return;const same=!!devSite&&devSignature===JSON.stringify(activeGeometry);const a=document.getElementById('siteReviewBtn'),b=document.getElementById('devNational'),c=document.getElementById('devSeoulReviewAction'),l=document.getElementById('devLandscapeReview');if(a){a.disabled=!same||devBusy||devSite?.region!=='SEOUL';}if(c){c.disabled=!same||devBusy||devSite?.region!=='SEOUL';}if(b)b.disabled=!same||devBusy;if(l)l.disabled=!same||devBusy;if(a)a.title=!same?'먼저 구역계를 확정하세요.':devSite?.region==='SEOUL'?'서울 대상지 확인됨':devSite?.region==='OUTSIDE_OR_MIXED'?'서울 외 또는 경계 혼합 대상지입니다.':'서울 행정경계 확인이 필요합니다.';if(devSite&&!same)devStatus('구역계 변경: 다시 확정하세요. 이전 검토창의 대상지는 유지됩니다.');}
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
// Selection context is VIEW only: no writes to selectedParcelPnus or planningAnalysis.
let devParcelContext=null,devParcelLabels=null,devParcelViewTimer=null,devParcelViewRevision=0,devParcelViewBusy=false,devParcelViewPending=false;
function devParcelViewGeometry(){
 const b=map.getBounds();return {type:'Polygon',coordinates:[[[b.getWest(),b.getSouth()],[b.getEast(),b.getSouth()],[b.getEast(),b.getNorth()],[b.getWest(),b.getNorth()],[b.getWest(),b.getSouth()]]]};
}
function devParcelViewStatus(text){const el=document.getElementById('devParcelViewStatus');if(el){el.textContent=text;el.hidden=!text;}}
function devParcelViewLabel(f){
 const p=Object.fromEntries(Object.entries(f.properties||{}).map(([k,v])=>[k.toLowerCase(),v]));
 if(p.jibun)return String(p.jibun);
 const pnu=String(p.pnu||f.id||'');if(!/^\d{19}$/.test(pnu))return '';
 const a=Number(pnu.slice(11,15)),b=Number(pnu.slice(15,19));return (pnu[10]==='2'?'산 ':'')+a+(b?'-'+b:'');
}
function devPaintParcelView(features){
 devParcelContext.clearLayers();devParcelLabels.clearLayers();const occupied=[];
 const valid=features.filter(f=>['Polygon','MultiPolygon'].includes(f.geometry?.type));
 devParcelContext.addData({type:'FeatureCollection',features:valid});
 for(const f of valid){
  const text=devParcelViewLabel(f);if(!text)continue;
  let point=f.properties?.label_point;
  if(!Array.isArray(point)){try{point=turf.pointOnFeature(f).geometry.coordinates;}catch(e){continue;}}
  const ll=L.latLng(point[1],point[0]);if(!map.getBounds().contains(ll))continue;
  const xy=map.latLngToContainerPoint(ll),width=Math.min(140,Math.max(36,text.length*8));
  if(occupied.some(r=>Math.abs(r.x-xy.x)<(r.width+width)/2+5&&Math.abs(r.y-xy.y)<23))continue;
  occupied.push({x:xy.x,y:xy.y,width});
  const span=document.createElement('span');span.textContent=text;
  L.marker(ll,{pane:'devParcelViewLabels',interactive:false,keyboard:false,icon:L.divIcon({className:'dev-parcel-number',html:span,iconSize:[width,22],iconAnchor:[width/2,11]})}).addTo(devParcelLabels);
 }
}
async function devLoadParcelView(){
 if(devMode!=='select'||!devParcelContext)return;
 if(devParcelViewBusy){devParcelViewPending=true;return;}
 const revision=devParcelViewRevision;
 if(!document.getElementById('devParcelViewToggle')?.checked){devParcelViewStatus('지적선·지번 표시 꺼짐');return;}
 if(map.getZoom()<16){devParcelViewStatus('지적선·지번을 보려면 지도를 확대하세요.');return;}
 const geometry=devParcelViewGeometry();
 if(turf.area(turf.feature(geometry))>10000000){devParcelViewStatus('조회 범위가 넓습니다. 지도를 확대하세요.');return;}
 devParcelViewBusy=true;devParcelViewPending=false;devParcelViewStatus('주변 지적선·지번 조회 중…');
 try{
  let v=await devApi('parcel-view',{geometry},30000);
  if(revision!==devParcelViewRevision)return;
  if(v.status==='UNKNOWN'&&!['RATE_LIMIT','TEMPORARILY_PAUSED','SERVICE_NOT_READY'].includes(v.error_code)&&devClientReady()){
   const r=(await devVWorldClientRequest(geometry,'LP_PA_CBND_BUBUN',1,1000))?.response;
   if(r?.status==='NOT_FOUND')v={status:'COMPLETE',features:[]};
   else if(r?.status==='OK'&&Array.isArray(r.result?.featureCollection?.features)){const fs=r.result.featureCollection.features;v={status:fs.length>=1000?'PARTIAL':'COMPLETE',features:fs};}
  }
  if(revision!==devParcelViewRevision)return;
  if(!['COMPLETE','PARTIAL'].includes(v.status))throw Error('지적 배경 미확보 · 다시 조회를 누르세요.');
  devPaintParcelView(v.features||[]);
  devParcelViewStatus(v.status==='PARTIAL'?'지적 일부 표시 · 확대하면 나머지 필지를 조회합니다.':v.features?.length?'지적선·지번 표시 · 필지 선택 모드에서 필지 안쪽을 클릭하세요.':'이 범위의 지적 도형을 확보하지 못했습니다.');
 }catch(e){if(revision===devParcelViewRevision)devParcelViewStatus('지적 배경 연결 실패 · 다시 조회를 누르세요.');}
 finally{devParcelViewBusy=false;if(devParcelViewPending||revision!==devParcelViewRevision)devScheduleParcelView();}
}
function devScheduleParcelView(){
 ++devParcelViewRevision;clearTimeout(devParcelViewTimer);
 if(devParcelContext)devParcelContext.clearLayers();if(devParcelLabels)devParcelLabels.clearLayers();
 devParcelViewTimer=setTimeout(devLoadParcelView,500);
}
function devInstallParcelView(){
 if(devMode!=='select'||devParcelContext)return;
 for(const [name,z] of [['devParcelViewLines',450],['devParcelViewLabels',610]]){const pane=map.getPane(name)||map.createPane(name);pane.style.zIndex=String(z);pane.style.pointerEvents='none';}
 devParcelContext=L.geoJSON(null,{pane:'devParcelViewLines',interactive:false,style:{color:'#fff',weight:1.4,opacity:.9,fill:false,interactive:false}}).addTo(map);
 devParcelLabels=L.layerGroup().addTo(map);
 const css=document.createElement('style');css.textContent='.dev-parcel-number{pointer-events:none!important;color:#fff;font:bold 12px/22px sans-serif;text-align:center;white-space:nowrap;text-shadow:-1px -1px 2px #111,1px 1px 2px #111,1px -1px 2px #111,-1px 1px 2px #111}.dev-parcel-view-tools{display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:8px 12px;background:#f8fafc;font-size:12px;color:#344054}.dev-parcel-view-tools input{width:auto}';document.head.append(css);
 const bar=document.createElement('div');bar.className='dev-parcel-view-tools';
 bar.innerHTML='<label><input id="devParcelViewToggle" type="checkbox" checked> 지적선·지번</label><button type="button" id="devParcelViewRetry">다시 조회</button><span id="devParcelViewStatus">지적 배경 준비 중</span>';
 map.getContainer().before(bar);
 document.getElementById('devParcelViewToggle').onchange=devScheduleParcelView;
 document.getElementById('devParcelViewRetry').onclick=devScheduleParcelView;
 // The existing map click lookup stays authoritative; labels cannot intercept clicks.
 map.on('movestart',()=>{++devParcelViewRevision;});map.on('moveend resize',devScheduleParcelView);
 devScheduleParcelView();
}

const devLayerSpecs=[
 ['LP_PA_CBND_BUBUN','연속지적','parcel'],
 ...['도시지역','관리지역','농림지역','자연환경보전지역'].map((n,i)=>['LT_C_UQ11'+(i+1),n,'zoning']),
 ...['도로','교통시설','공간시설','유통·공급시설','공공·문화체육시설','방재시설','보건위생시설','환경기초시설','기타 기반시설'].map((n,i)=>['LT_C_UPISUQ15'+(i+1),n,'facility']),
 ...[['121','경관지구'],['123','고도지구'],['124','방화지구'],['125','방재지구'],['126','보호지구'],['128','취락지구'],['129','개발진흥지구'],['130','특정용도제한지구']].map(([k,n])=>['LT_C_UQ'+k,n,'district']),
 ['LT_C_UD801','개발제한구역','area'],
 ['LT_C_UPISUQ161','지구단위계획구역','area']];
const devFailureLabels={CONNECT_TIMEOUT:'제공기관 연결 시간 초과',READ_TIMEOUT:'제공기관 응답 시간 초과',TIMEOUT:'공공자료 조회 시간 초과',TLS_ERROR:'제공기관 보안 연결 실패',PROXY_ERROR:'중계 연결 실패',CONNECTION_ERROR:'제공기관 연결 실패',PROVIDER_REDIRECT:'제공기관 응답 주소 변경 확인 필요',PROVIDER_HTTP_ERROR:'제공기관 HTTP 오류',PLATFORM_TIMEOUT:'플랫폼 서버 응답 시간 초과',PLATFORM_CONNECTION:'플랫폼 서버 연결 실패',PLATFORM_HTTP_ERROR:'플랫폼 요청 처리 오류',PLATFORM_FORMAT:'플랫폼 응답 형식 오류',PLATFORM_BUSY:'플랫폼에서 다른 조회 처리 중',FACT_SYNC_FAILED:'보조자료를 보고서에 연결하지 못함',SERVICE_NOT_READY:'자료 연결 설정 확인 필요',TEMPORARILY_PAUSED:'외부 연결 재시도 대기',RATE_LIMIT:'제공기관 호출 한도',PROVIDER_REJECTED:'제공기관 요청 거부',PROVIDER_UNREACHABLE:'제공기관 연결 실패',PROVIDER_FORMAT:'자료 응답 형식 확인 필요',REQUEST_FAILED:'자료 조회 실패',DEFERRED:'연결 복구 후 조회',CLIENT_AUTH:'VWorld 인증 또는 허용 도메인 확인 필요',CLIENT_REQUEST:'VWorld 조회 조건 확인 필요',CLIENT_PROVIDER_REJECTED:'VWorld 데이터 요청 거부',CLIENT_TIMEOUT:'브라우저 보조 조회 시간 초과',CLIENT_NETWORK:'브라우저 보조 연결 실패',CLIENT_CONNECTION_UNAVAILABLE:'브라우저용 연결을 사용할 수 없음',CLIENT_PROVIDER_FORMAT:'VWorld 응답 형식 확인 필요'};
function devHasFact(value){return !!value&&['COMPLETE','REFERENCE'].includes(value.status);}
function devOutcome(facts){
 const complete=facts.filter(v=>v.status==='COMPLETE').length,reference=facts.filter(v=>v.status==='REFERENCE').length,deferred=facts.filter(v=>v.status==='DEFERRED').length,failed=facts.length-complete-reference-deferred;
 return {complete,reference,failed,deferred,text:!facts.length?'자료 조회 대기':`서버 확보 ${complete}개 · 보조 참고 ${reference}개 · 조회 실패 ${failed}개 · 미조회 ${deferred}개`};
}
async function devSyncStoredFacts(){
 const stored=await devApi('snapshots/'+encodeURIComponent(devSite.id)+'/layers');
 devFacts=devLayerSpecs.map(([key])=>stored.layers[key]).filter(Boolean);
 devRenderFacts();
 return stored;
}
function devFailureDescription(v){
 if(v.status==='DEFERRED')return '미조회 · '+(devFailureLabels[v.deferred_reason]||'재시도 필요');
 return '서버: '+(devFailureLabels[v.error_code]||'자료 조회 실패')+(v.client_error_code?' · 보조 연결: '+(devFailureLabels[v.client_error_code]||'조회 실패'):'');
}
const devRetryWait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
let devAerialFactStamp='';
function devDrawAerialFacts(){
 if(devMode!=='development'||typeof map==='undefined')return;
 const stamp=JSON.stringify(devFacts.map(v=>[v.layer,v.fact_revision,v.status,v.queried_at]));
 if(stamp===devAerialFactStamp)return;devAerialFactStamp=stamp;
 if(window.devFactLayer)map.removeLayer(window.devFactLayer);window.devFactLayer=L.layerGroup().addTo(map);
 for(const v of devFacts){if(!devHasFact(v))continue;
  L.geoJSON(v.features,{style:{color:v.layer==='LP_PA_CBND_BUBUN'?'#64748b':v.layer.startsWith('LT_C_UQ11')?'#087f8c':'#bd691c',weight:1,fillOpacity:.10},onEachFeature:(f,l)=>{const p=document.createElement('pre');p.textContent=(v.status==='REFERENCE'?'보조 조회 참고자료 · 출처 재확인 필요\n':'')+JSON.stringify(f.properties,null,2);l.bindPopup(p);}}).addTo(window.devFactLayer);
 }
}
function devRenderFacts(){
 devDrawAerialFacts();
 if(typeof devPlanningMap!=="undefined"&&devPlanningMap){try{devPlanningRefresh();}catch(e){const el=document.getElementById("devPlanningStatus");if(el)el.textContent="일부 도형 표시 오류 · 오른쪽 조회결과를 확인하세요.";}}
 for(const group of ['parcel','zoning','district','area','facility']){
 const host=document.getElementById('devFacts-'+group);if(!host)continue;host.replaceChildren();
 for(const [id,name,g] of devLayerSpecs.filter(v=>v[2]===group)){
 const v=devFacts.find(f=>f.layer===id),row=document.createElement('div');row.className='dev-fact-row';
 const title=document.createElement('strong');title.textContent=name;
 const badge=document.createElement('span');badge.className='dev-state '+(!v?'waiting':v.status==='COMPLETE'?'ok':'failed');badge.textContent=!v?'대기':v.status==='REFERENCE'?'보조 참고':v.status==='COMPLETE'?(v.features.length?'확보':'조회 0건'):v.status==='DEFERRED'?'미조회':'조회 실패';row.append(title,badge);
 const desc=document.createElement('p');desc.textContent=!v?'순차 조회 예정':devHasFact(v)?v.features.length+'건 · 대상지 중첩 '+v.overlap_area_m2.toLocaleString()+'㎡ · '+(v.status==='REFERENCE'?'보조자료 · 원문 진위·전체 수록 여부 미검증':'VWorld 서버 조회')+(v.refresh_error_code?' · 재조회 실패, 이전 확보자료 표시':''):devFailureDescription(v);row.append(desc);
 if(devHasFact(v)&&v.features.length){const names=[...new Set(v.features.map(f=>{const p=f.properties||{},lower=Object.fromEntries(Object.entries(p).map(([k,v])=>[k.toLowerCase(),v]));return lower.fclty_nm||lower.uname||lower.name||lower.jibun||lower.dgm_nm||'';}).filter(Boolean))];if(names.length){const line=document.createElement('p');line.className='dev-feature-names';line.textContent=names.slice(0,8).join(' · ')+(names.length>8?' 외 '+(names.length-8)+'건':'');row.append(line);}}
 host.append(row);
 }
 }
 const outcome=devOutcome(devFacts);const summary=document.getElementById('devConnectionSummary');if(summary){summary.textContent=devBusy?'자료 조회 중 · '+(outcome.complete+outcome.reference)+'개 항목 수신 / '+devFacts.length+'개 항목 확인':outcome.text+' · '+(outcome.complete+outcome.reference)+'/'+devLayerSpecs.length+' 항목 수신';summary.className=outcome.failed||outcome.deferred||outcome.reference||outcome.complete===0?'dev-alert':'dev-alert dev-success';}
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
  try{server=await devApi('layer',{geometry,layer,snapshot_id:devSite?.id},42000);}catch(e){server={status:'UNKNOWN',error_code:e.code||'PLATFORM_CONNECTION'};}
  if(devHasFact(server))return server;
  if(server.error_code!=='TEMPORARILY_PAUSED'||attempt===2)break;
  const seconds=Number(server.retry_after_seconds);
  await devRetryWait(Math.max(1,Math.min(15,Number.isFinite(seconds)?seconds:1))*1000);
 }
 if(['RATE_LIMIT','SERVICE_NOT_READY','TEMPORARILY_PAUSED','PROVIDER_REJECTED'].includes(server.error_code))return {...server,layer,name,source:'VWorld',features:[]};
 if(devLayerSpecs.find(x=>x[0]===layer)?.[2]==='district'||devLayerSpecs.find(x=>x[0]===layer)?.[2]==='area')
  return {...server,layer,name,source:'VWorld',features:[],client_error_code:'SERVER_ONLY_LAYER'};
 if(!devClientReady())return {...server,layer,name,source:'VWorld',features:[],client_error_code:'CLIENT_CONNECTION_UNAVAILABLE'};
 try{
  const client=await devBrowserLayer(layer,geometry);
  try{return await devApi('snapshots/'+encodeURIComponent(devSite.id)+'/browser-layer',{geometry,layer,features:client.features});}
  catch(e){return {status:'UNKNOWN',layer,name,source:'VWorld',features:[],error_code:'FACT_SYNC_FAILED',client_error_code:e.code||'PLATFORM_CONNECTION'};}
 }
 catch(e){const code=e.code||e.message;const safe=Object.hasOwn(devFailureLabels,code)?code:'CLIENT_PROVIDER_FORMAT';return {status:'UNKNOWN',layer,name,source:'VWorld',features:[],error_code:server.error_code,client_error_code:safe};}
}
async function devLoadFacts(manual=false){
 if(devBusy||devInventoryBusy||!devSite)return;devBusy=true;devFacts=[];devRenderFacts();
 const retry=document.getElementById('devFetch');if(retry)retry.disabled=true;
 const state={};let terminalFailure=null;
 try{for(let i=0;i<devLayerSpecs.length;i++){
 const [layer,name,group]=devLayerSpecs[i];devStatus('공간자료 확인 중 · '+(i+1)+'/'+devLayerSpecs.length);
 if(group==='district'||group==='area'){
  // These official layers are acquired by the report source jobs.
  // Do not duplicate a scarce upstream request before that workflow.
  devFacts.push({layer,name,status:'DEFERRED',features:[],error_code:'DEFERRED'});devRenderFacts();continue;
 }
 if(terminalFailure){devFacts.push({layer,name,status:'DEFERRED',features:[],error_code:'DEFERRED',deferred_reason:terminalFailure});continue;}
 try{const v=await devFetchNationalLayer(layer,devSite.geometry,state);devFacts.push(v);
 if(!devHasFact(v)&&['RATE_LIMIT','SERVICE_NOT_READY'].includes(v.error_code))terminalFailure=v.error_code;
 else if(v.client_error_code==='RATE_LIMIT')terminalFailure='RATE_LIMIT';
 }catch(e){devFacts.push({layer,name,status:'UNKNOWN',features:[],error_code:'REQUEST_FAILED'});}
 devRenderFacts();}
 }finally{devBusy=false;if(retry)retry.disabled=false;devRenderFacts();devStatus(devOutcome(devFacts).text+' · 사업 적합성은 별도 검토입니다.');}
}
// National planning-map symbols from one server-side verified catalog.
// The source classification remains independent of the display style.
let devPlanningCatalog=null;
const devPlanningAciFallback={2:'#ffff00',40:'#ffbf00',51:'#ffff7f',71:'#bfff7f',131:'#7fffff',191:'#bf7fff',231:'#ff7fbf'};
const devPlanningPatternColor={red:'#ff0000',green:'#00a000',blue:'#0000ff',dark_purple:'#660099',light_orange:'#ffbf7f',gray:'#cccccc',light_blue:'#7fffff'};
const devPlanningFacilityColors=['#b7ada4','#bdadd4','#569c68','#56adbd','#7da6d1','#d29bba','#c99b6c','#88a6a1','#a3a8ad'];
const DEV_PLANNING_FILL_OPACITY=1;
let devPlanningMap=null,devPlanningGroups=null,devPlanningStamp='',devPlanningLabels=[];
let devPlanningParcelLines=null,devPlanningParcelText=null,devPlanningParcelTimer=null;
let devPlanningParcelRevision=0,devPlanningParcelBusy=false,devPlanningParcelPending=false;
const DEV_PLANNING_ZONING_LAYERS=['LT_C_UQ111','LT_C_UQ112','LT_C_UQ113','LT_C_UQ114'];
let devPlanningViewportZoning=new Map(),devPlanningZoningRevision=0,devPlanningZoningTimer=null;
let devPlanningZoningBusy=false,devPlanningZoningPending=false,devPlanningZoningState='대상지 용도지역 표시';

async function devPlanningLoadSymbolCatalog(){
 try{
  const rsp=await fetch('/api/development/planning-symbols',{cache:'no-store'});
  if(!rsp.ok)throw Error('심벌 목록 조회 실패');
  const catalog=await rsp.json();
  if(catalog?.schema_version!==1||!Array.isArray(catalog.types))throw Error('심벌 목록 형식 오류');
  devPlanningCatalog=catalog;devPlanningStamp='';
  if(devPlanningMap)devPlanningRefresh();
 }catch(e){
  const el=document.getElementById('devPlanningStatus');
  if(el)el.textContent='지침 심벌 목록 미확보 · 임의의 용도지역 색상으로 대체하지 않습니다.';
 }
}
function devPlanningSourceGroup(id){
 if(id==='LP_PA_CBND_BUBUN')return 'parcel';
 if(/^LT_C_UQ11[1-4]$/.test(id))return 'zoning';
 if(/^LT_C_UQ12/.test(id)||id==='LT_C_UQ130')return 'district';
 if(id==='LT_C_UD801'||id==='LT_C_UPISUQ161')return 'area';
 return 'facility';
}
function devPlanningOfficialSymbol(name,group){
 const normalized=String(name||'').replace(/\s+/g,'').replace(/ㆍ/g,'·');
 let candidates=(devPlanningCatalog?.types||[]).filter(v=>v.group===group);
 const hits=[];
 for(const spec of candidates)for(const word of [spec.name,...(spec.aliases||[])]){
  const text=String(word).replace(/\s+/g,'');
  if(text&&normalized.includes(text))hits.push([text.length,spec]);
 }
 hits.sort((a,b)=>b[0]-a[0]);
 return hits[0]?.[1]||null;
}
function devPlanningSymbol(id,feature){
 const p=Object.fromEntries(Object.entries(feature.properties||{}).map(([k,v])=>[k.toLowerCase(),v]));
 const name=String(p.uname||p.dgm_nm||p.fclty_nm||p.name||p.jibun||'').trim();
 const group=devPlanningSourceGroup(id);
 if(group==='parcel')return {name:String(p.jibun||''),label:'지적경계',color:'#64748b',fillOpacity:0,weight:1,fill:false,pane:'devParcel'};
 const spec=devPlanningOfficialSymbol(name,group);
 if(!spec){
  return {name,label:name||'세부종류 미확인',color:'#596579',fillColor:'#ced5d9',
   fillOpacity:.2,weight:1,pane:'dev'+group,unverified:true};
 }
 const aci=spec.fill_aci;
 const fillColor=(devPlanningCatalog?.aci_rgb||devPlanningAciFallback)[String(aci)]||'#ffffff';
 const pattern=spec.pattern_type||null;
 const roadGradeUnknown=group==='facility'&&spec.name==='도로'&&!/(광로|대로)/.test(name);
 const isLine=['LineString','MultiLineString'].includes(feature.geometry?.type);
 const color=roadGradeUnknown&&isLine?'#64748b':
   (devPlanningPatternColor[spec.line_color]||devPlanningPatternColor[spec.boundary_color]||'#746b60');
 const patternColor=devPlanningPatternColor[spec.pattern_color]||'#ff0000';
 const usesAreaPattern=['diagonal','vertical','circles'].includes(pattern);
 const noBase=spec.fill_mode==='none'||spec.fill_mode==='inherit';
 return {name,label:roadGradeUnknown&&isLine?(name||spec.name)+' · 도로등급 미확인':(name||spec.name),color,fillColor,aci,pattern,patternColor,
  fillMode:spec.fill_mode,noBase,labelColor:devPlanningPatternColor[spec.label_color]||'#111827',
  spacingMm:spec.spacing_mm||5,diameterMm:spec.diameter_mm||2,
  fillOpacity:spec.fill_mode==='solid'||usesAreaPattern?1:0,
  weight:group==='facility'?1.2:.8,pane:'dev'+group,
  boundaryPattern:spec.boundary_pattern||null,
  dashArray:spec.boundary_pattern==='dots'?'2 8':pattern==='edge_ticks'?'5 4':pattern==='line_dots'?'2 12':null,
  unverified:false};
}
function devPlanningApplyPattern(layer,symbol){
 if(!['diagonal','vertical','circles'].includes(symbol.pattern)||!layer._path?.ownerSVGElement)return;
 const svg=layer._path.ownerSVGElement,ns='http://www.w3.org/2000/svg';
 const documentRef=svg.ownerDocument;
 const id='dev-pattern-'+[symbol.pattern,symbol.fillColor,symbol.patternColor,symbol.spacingMm,symbol.diameterMm,symbol.fillMode].join('-').replace(/[^a-zA-Z0-9-]/g,'');
 let defs=svg.querySelector('defs');
 if(!defs){defs=documentRef.createElementNS(ns,'defs');svg.insertBefore(defs,svg.firstChild);}
 if(!defs.querySelector('[id="'+id+'"]')){
  const pattern=documentRef.createElementNS(ns,'pattern');
  pattern.setAttribute('id',id);pattern.setAttribute('patternUnits','userSpaceOnUse');
  // Browser legend is a screen preview; actual 1:5,000 spacing is applied in CAD.
  const pitch=Math.max(4,Number(symbol.spacingMm||5)*3.7795);
  pattern.setAttribute('width',String(pitch));pattern.setAttribute('height',String(pitch));
  const add=(tag,attrs)=>{const el=documentRef.createElementNS(ns,tag);for(const [k,v] of Object.entries(attrs))el.setAttribute(k,String(v));pattern.append(el);};
  if(!symbol.noBase)add('rect',{width:pitch,height:pitch,fill:symbol.fillColor});
  if(symbol.pattern==='diagonal')add('path',{d:'M0 '+pitch+' L'+pitch+' 0',fill:'none',stroke:symbol.patternColor,'stroke-width':1});
  else if(symbol.pattern==='vertical')add('path',{d:'M'+(pitch/2)+' 0 L'+(pitch/2)+' '+pitch,fill:'none',stroke:symbol.patternColor,'stroke-width':1});
  else if(symbol.pattern==='circles')add('circle',{cx:pitch/2,cy:pitch/2,r:Math.max(1,Number(symbol.diameterMm||2)*3.7795/2),stroke:symbol.patternColor,fill:'none','stroke-width':1});
  else if(symbol.pattern==='edge_ticks')add('path',{d:'M0 0 L0 3',stroke:symbol.patternColor,'stroke-width':1});
  defs.append(pattern);
 }
 layer._path.setAttribute('fill','url(#'+id+')');
 layer._path.setAttribute('fill-opacity','1');
}

/* Viewport cadastral context only. This NEVER alters the selected site or FACT. */
function devPlanningParcelStatus(message){
 const status=document.getElementById('devPlanningParcelStatus');
 if(status)status.textContent=message;
}
function devPlanningParcelBounds(){
 const b=devPlanningMap.getBounds();
 return {type:'Polygon',coordinates:[[[b.getWest(),b.getSouth()],[b.getEast(),b.getSouth()],
 [b.getEast(),b.getNorth()],[b.getWest(),b.getNorth()],[b.getWest(),b.getSouth()]]]};
}
function devPlanningDrawParcelLabels(features){
 devPlanningParcelText.clearLayers();
 if(devPlanningMap.getZoom()<16)return;
 const used=[],bounds=devPlanningMap.getBounds();
 for(const feature of features){
  if(used.length>=160)break;
  const value=devParcelViewLabel(feature);
  if(!value)continue;
  let point=feature.properties?.label_point;
  if(!Array.isArray(point)){try{point=turf.pointOnFeature(feature).geometry.coordinates;}catch(_){continue;}}
  const ll=L.latLng(point[1],point[0]);
  if(!bounds.contains(ll))continue;
  const pix=devPlanningMap.latLngToContainerPoint(ll);
  const width=Math.min(120,Math.max(34,value.length*8));
  if(used.some(p=>Math.abs(p.x-pix.x)<(p.w+width)/2+5&&Math.abs(p.y-pix.y)<20))continue;
  used.push({x:pix.x,y:pix.y,w:width});
  const label=document.createElement('span');label.textContent=value;
  L.marker(ll,{pane:'devParcel',keyboard:false,interactive:false,
    icon:L.divIcon({className:'dev-plan-parcel-context-label',html:label,iconSize:[width,18],iconAnchor:[width/2,9]})}).addTo(devPlanningParcelText);
 }
}
async function devPlanningFetchViewportParcels(){
 if(!devPlanningMap||!devPlanningParcelLines||devMode!=='development')return;
 const revision=devPlanningParcelRevision;
 if(!devPlanningMap.hasLayer(devPlanningGroups.parcel)){
  devPlanningParcelStatus('지적경계 표시 꺼짐');return;
 }
 if(devPlanningParcelBusy){devPlanningParcelPending=true;return;}
 if(devPlanningMap.getZoom()<15){
  devPlanningParcelLines.clearLayers();devPlanningParcelText.clearLayers();
  devPlanningParcelStatus('주변 지적선은 지도를 확대하면 표시됩니다.');return;
 }
 const geometry=devPlanningParcelBounds();
 if(turf.area(turf.feature(geometry))>5000000){
  devPlanningParcelLines.clearLayers();devPlanningParcelText.clearLayers();
  devPlanningParcelStatus('화면 범위가 넓어 지적 조회를 보류했습니다. 지도를 확대하세요.');return;
 }
 devPlanningParcelBusy=true;devPlanningParcelPending=false;
 devPlanningParcelStatus('화면 전체 주변 연속지적 조회 중…');
 try{
  let result=await devApi('parcel-view',{geometry},30000);
  if(revision!==devPlanningParcelRevision)return;
  if(result.status==='UNKNOWN'&&!['RATE_LIMIT','TEMPORARILY_PAUSED','SERVICE_NOT_READY'].includes(result.error_code)&&devClientReady()){
   try{
    const rsp=(await devVWorldClientRequest(geometry,'LP_PA_CBND_BUBUN',1,1000))?.response;
    if(rsp?.status==='NOT_FOUND')result={status:'COMPLETE',features:[]};
    else if(rsp?.status==='OK'&&Array.isArray(rsp.result?.featureCollection?.features)){
     const features=rsp.result.featureCollection.features;
     result={status:features.length>=1000?'PARTIAL':'COMPLETE',features};
    }
   }catch(_){/* Keep original failure if browser fallback fails. */}
  }
  if(revision!==devPlanningParcelRevision)return;
  if(!['COMPLETE','PARTIAL'].includes(result.status))throw Error('CADASTRE_UNAVAILABLE');
  const features=(result.features||[]).filter(f=>f?.geometry&&['Polygon','MultiPolygon'].includes(f.geometry.type));
  devPlanningParcelLines.clearLayers();
  devPlanningParcelLines.addData({type:'FeatureCollection',features});
  devPlanningDrawParcelLabels(features);
  devPlanningUpdateLabels();
  devPlanningParcelStatus(result.status==='PARTIAL'
   ?'주변 연속지적 '+features.length+'필지 일부 표시 · 조회 한도에 도달했습니다. 확대하여 조회하세요.'
   :features.length?'화면 주변 연속지적 '+features.length+'필지 표시':'주변 지적 조회 0건 · 수록 여부 확인 필요');
 }catch(_){
  if(revision===devPlanningParcelRevision){
   devPlanningParcelLines.clearLayers();devPlanningParcelText.clearLayers();
   devPlanningParcelStatus('주변 지적 조회 실패 · 대상지 지적만 표시. 이동·확대 시 재조회합니다.');
  }
 }finally{
  devPlanningParcelBusy=false;
  if(devPlanningParcelPending||revision!==devPlanningParcelRevision){
   devPlanningParcelPending=false;devPlanningScheduleViewportParcels();
  }
 }
}
function devPlanningViewportStatus(message){
 devPlanningZoningState=message;
 const el=document.getElementById('devPlanningZoningStatus');
 if(el)el.textContent=message;
}
async function devPlanningFetchViewportZoning(){
 if(!devPlanningMap||devMode!=='development'||!devPlanningGroups)return;
 const revision=devPlanningZoningRevision;
 if(!devPlanningMap.hasLayer(devPlanningGroups.zoning)){
  devPlanningViewportStatus('주변 용도지역 표시 꺼짐');return;
 }
 if(devPlanningZoningBusy){devPlanningZoningPending=true;return;}
 if(devPlanningMap.getZoom()<15){
  devPlanningViewportZoning.clear();devPlanningStamp='';devPlanningRefresh();
  devPlanningViewportStatus('주변 용도지역은 확대하면 조회합니다. 현재 대상지 교차 자료만 표시합니다.');return;
 }
 const geometry=devPlanningParcelBounds();
 if(turf.area(turf.feature(geometry))>5000000){
  devPlanningViewportZoning.clear();devPlanningStamp='';devPlanningRefresh();
  devPlanningViewportStatus('조회범위가 5㎢를 초과합니다. 확대하면 주변 용도지역을 조회합니다.');return;
 }
 devPlanningZoningBusy=true;devPlanningZoningPending=false;
 devPlanningViewportStatus('지도 화면 전체의 용도지역 조회 중…');
 const results=new Map();let ok=0,partial=0,failed=0,diag=[];
 try{
  for(const layer of DEV_PLANNING_ZONING_LAYERS){
   if(revision!==devPlanningZoningRevision)break;
   try{
    const result=await devApi('planning-view',{geometry,layer},35000);
    if(result.status==='COMPLETE'){results.set(layer,result);ok++;}
    else if(result.status==='PARTIAL'){results.set(layer,result);partial++;}
    else failed++;
    if(layer==='LT_C_UQ111'){
     const counts=result.subtype_counts||{};
     const third=Object.entries(counts).filter(([k])=>/제?3종일반주거|제?삼종일반주거/.test(k.replace(/\s/g,''))).reduce((a,[,n])=>a+Number(n||0),0);
     diag.push('도시지역 원자료 '+(result.received??'?')+'건 · 정상 도형 '+(result.count??0)+'건 · 제3종일반주거 '+third+'건'+
      ((result.skipped_invalid_features||0)?' · 오류 도형 제외 '+result.skipped_invalid_features+'건':'')+
      ((result.repaired_for_map_only||0)?' · 표시용 보정 '+result.repaired_for_map_only+'건':'')+
      (result.error_code?' · 오류 '+(devFailureLabels[result.error_code]||result.error_code):''));
    }
   }catch(e){
    failed++;
    if(layer==='LT_C_UQ111')diag.push('도시지역 조회 실패 · '+(devFailureLabels[e.code]||'연결 확인 필요'));
   }
  }
  if(revision!==devPlanningZoningRevision)return;
  devPlanningViewportZoning=results;
  devPlanningStamp='';devPlanningRefresh();
  devPlanningViewportStatus(
   '주변 용도지역 '+ok+'개 레이어 조회완료'
   +(partial?' · 일부자료 '+partial+'개':'')
   +(failed?' · 미확보 '+failed+'개(해당 레이어는 대상지 교차자료만 표시)':'')
   +(diag.length?' · '+diag.join(' · '):'')
   +' · 화면 범위 전용, 현황보고서 FACT는 변경하지 않음'
  );
 }finally{
  devPlanningZoningBusy=false;
  if(devPlanningZoningPending||revision!==devPlanningZoningRevision){
   devPlanningZoningPending=false;devPlanningScheduleViewportZoning();
  }
 }
}
function devPlanningScheduleViewportZoning(){
 ++devPlanningZoningRevision;
 if(devPlanningZoningTimer)clearTimeout(devPlanningZoningTimer);
 if(!devPlanningMap)return;
 devPlanningViewportZoning.clear();devPlanningStamp='';devPlanningRefresh();
 devPlanningZoningTimer=setTimeout(devPlanningFetchViewportZoning,850);
}
function devPlanningScheduleViewportParcels(){
 ++devPlanningParcelRevision;
 if(devPlanningParcelTimer)clearTimeout(devPlanningParcelTimer);
 if(devPlanningMap)devPlanningParcelTimer=setTimeout(devPlanningFetchViewportParcels,550);
}

// A viewport query supplements, never replaces, authoritative site-bound FACT.
// Incomplete provider viewport results must not erase already collected subtypes.
function devPlanningMergeDisplayFacts(siteFacts,viewportZoning){
 const merged=siteFacts.filter(f=>!viewportZoning.has(f.layer));
 for(const [layer,viewport] of viewportZoning){
  const site=siteFacts.find(f=>f.layer===layer&&devHasFact(f));
  if(!site||!site.features?.length){merged.push(viewport);continue;}
  const features=[],ids=new Map();
  const stableId=f=>{
   const p=f.properties||{};
   const key=f.id||p.unq_mnno||p.UNQ_MNNO||p.mnum||p.MNUM;
   return key==null||String(key).trim()===''?null:String(key);
  };
  const add=(f,preferSite=false)=>{
   const id=stableId(f),index=id==null?undefined:ids.get(id);
   if(index!==undefined){if(preferSite)features[index]=f;}
   else{if(id!=null)ids.set(id,features.length);features.push(f);}
  };
  for(const f of viewport.features||[])add(f);
  for(const f of site.features)add(f,true);
  merged.push({...viewport,features,
   preserved_site_fact_revision:site.fact_revision||site.queried_at||null,
   preserved_site_features:site.features.length});
 }
 return merged;
}
function devPlanningRefresh(){
 if(!devPlanningMap)return;
 const status=document.getElementById('devPlanningStatus');
 const complete=devFacts.filter(f=>f.status==='COMPLETE').length,reference=devFacts.filter(f=>f.status==='REFERENCE').length;
 status.textContent=`${devBusy?'자료 조회 중':'조회 종료'} · 서버 확보 ${complete}개 · 보조 참고 ${reference}개 / ${devLayerSpecs.length}개 · 대상지와 교차하는 확보 도형 표시. 빈 부분은 시설 없음이 아닙니다.`;
 const drawFacts=devPlanningMergeDisplayFacts(devFacts,devPlanningViewportZoning);
 const stamp=JSON.stringify(drawFacts.map(f=>[f.layer,f.fact_revision,f.status,f.queried_at,f.features?.length,f.truncated,f.preserved_site_fact_revision,f.preserved_site_features]));
 if(stamp===devPlanningStamp)return;devPlanningStamp=stamp;
 for(const group of Object.values(devPlanningGroups))group.clearLayers();devPlanningLabels=[];
 if(devPlanningParcelLines)devPlanningGroups.parcel.addLayer(devPlanningParcelLines);
 if(devPlanningParcelText)devPlanningGroups.parcel.addLayer(devPlanningParcelText);
 const legend=document.getElementById('devPlanningLegend');legend.replaceChildren();
 const symbols=new Map([['확정 구역계',{color:'#dc2626'}]]);
 for(const fact of drawFacts){
  if(!devHasFact(fact)&&fact.status!=='PARTIAL')continue;
  for(const f of fact.features||[]){
   if(!f.geometry)continue;
   const symbol=devPlanningSymbol(fact.layer,f);symbols.set(symbol.label,symbol);
   const group=devPlanningSourceGroup(fact.layer);
   L.geoJSON(f,{pane:symbol.pane,style:()=>symbol,onEachFeature:(_,layer)=>{
    if(['diagonal','vertical','circles'].includes(symbol.pattern))layer.on('add',()=>devPlanningApplyPattern(layer,symbol));
    const text=document.createElement('span');text.textContent=symbol.name||symbol.label;
    if(!symbol.unverified)text.style.color=symbol.labelColor||'#111827';
    layer.bindTooltip(text,{direction:'center',className:'dev-plan-label',permanent:false});
    const popup=document.createElement('div');popup.textContent=`${symbol.label}${symbol.name&&symbol.name!==symbol.label?' · '+symbol.name:''} · ${fact.status==='REFERENCE'?'보조자료 · 출처 재확인 필요':'VWorld 서버 수신'}`;
    layer.bindPopup(popup);
    if(symbol.name&&layer.getBounds)devPlanningLabels.push({layer,text,symbol,group});
   }}).addTo(devPlanningGroups[group]);
  }
 }
 for(const [name,symbol] of symbols){
  const item=document.createElement('span'),swatch=document.createElement('i');
  swatch.style.backgroundColor=symbol.noBase?'transparent':(symbol.fillColor||symbol.color);
  const lineColor=symbol.patternColor||'#008000';
  if(symbol.pattern==='diagonal')swatch.style.backgroundImage='repeating-linear-gradient(135deg,transparent 0px,transparent 4px,'+lineColor+' 4px,'+lineColor+' 5px)';
  if(symbol.pattern==='vertical')swatch.style.backgroundImage='repeating-linear-gradient(90deg,transparent 0px,transparent 5px,'+lineColor+' 5px,'+lineColor+' 6px)';
  if(symbol.pattern==='circles')swatch.style.backgroundImage='radial-gradient(transparent 33%,'+lineColor+' 35%,'+lineColor+' 39%,transparent 42%)';
  if(symbol.boundaryPattern==='dots'||symbol.pattern==='edge_ticks')swatch.style.border='1px dashed '+(symbol.color||lineColor);
  if(symbol.pattern==='line_dots')swatch.style.backgroundImage='repeating-linear-gradient(90deg,'+lineColor+' 0px,'+lineColor+' 2px,transparent 2px,transparent 8px)';
  item.append(swatch,document.createTextNode(name));legend.append(item);
 }
 devPlanningUpdateLabels();
}
function devPlanningUpdateLabels(){
 if(!devPlanningMap)return;
 const used=new Set();let shown=0;
 for(const entry of devPlanningLabels){
  const {layer,text,group}=entry;
  const point=layer.getBounds().getCenter(),pixel=devPlanningMap.latLngToContainerPoint(point);
  const key=Math.floor(pixel.x/110)+':'+Math.floor(pixel.y/28);
  const visible=devPlanningMap.hasLayer(devPlanningGroups[group])&&devPlanningMap.getBounds().contains(point)&&devPlanningMap.getZoom()>=16&&!used.has(key)&&shown<150&&!(group==='parcel'&&devPlanningParcelLines?.getLayers().length);
  layer.unbindTooltip();layer.bindTooltip(text,{direction:'center',className:'dev-plan-label',permanent:visible});
  if(visible){used.add(key);shown++;}
 }
}
function devInstallPlanningMap(){
 if(devMode!=='development'||devPlanningMap)return;
 const target=document.querySelector('.service-target-box');if(!target)return;
 const box=document.createElement('section');box.id='devPlanningBox';
 box.innerHTML='<h3>도시계획정보도 <small>항공사진과 위치·확대 동기화</small></h3><div id="devPlanningMap" aria-label="도시계획정보도"></div><div id="devPlanningLegend"></div><p id="devPlanningStatus" role="status">자료 조회 대기</p><p id="devPlanningParcelStatus" role="status">주변 지적 조회 대기</p><p id="devPlanningZoningStatus" role="status">주변 용도지역 조회 대기</p><p class="dev-plan-source">자료: VWorld · 도시·군관리계획수립지침 별첨 1 ACI 색상·무늬 표기(웹은 화면규격 미리보기) · 법정 고시도면은 아닙니다.</p>';
 target.append(box);
 devPlanningMap=L.map('devPlanningMap',{attributionControl:true,zoomControl:true,crs:map.options.crs,zoomSnap:map.options.zoomSnap,minZoom:map.getMinZoom(),maxZoom:map.getMaxZoom()});
 for(const [pane,z] of [['devzoning',410],['devdistrict',415],['devarea',418],['devfacility',420],['devParcel',430],['devBoundary',440]])devPlanningMap.createPane(pane).style.zIndex=z;
 devPlanningGroups={zoning:L.layerGroup().addTo(devPlanningMap),district:L.layerGroup().addTo(devPlanningMap),area:L.layerGroup().addTo(devPlanningMap),facility:L.layerGroup().addTo(devPlanningMap),parcel:L.layerGroup().addTo(devPlanningMap)};
 devPlanningParcelLines=L.geoJSON(null,{pane:'devParcel',interactive:false,
  style:{color:'#50677b',weight:1.1,opacity:.9,fill:false}});
 devPlanningParcelText=L.layerGroup();
 devPlanningGroups.parcel.addLayer(devPlanningParcelLines);
 devPlanningGroups.parcel.addLayer(devPlanningParcelText);
 L.geoJSON(devSite.geometry,{pane:'devBoundary',interactive:false,style:{color:'#dc2626',weight:3,fillOpacity:0}}).addTo(devPlanningMap);
 L.control.layers(null,{'용도지역':devPlanningGroups.zoning,'용도지구':devPlanningGroups.district,'용도구역':devPlanningGroups.area,'도시계획시설':devPlanningGroups.facility,'지적경계':devPlanningGroups.parcel},{collapsed:false}).addTo(devPlanningMap);
 L.control.scale({imperial:false}).addTo(devPlanningMap);
 const north=L.control({position:'topright'});north.onAdd=()=>{const el=L.DomUtil.create('div','dev-plan-north');el.textContent='↑ N';return el;};north.addTo(devPlanningMap);
 devPlanningMap.attributionControl.addAttribution('VWorld 공간정보');
 let syncing=false;
 const sync=(from,to)=>{if(syncing)return;syncing=true;try{to.setView(from.getCenter(),from.getZoom(),{animate:false});}finally{syncing=false;}};
 map.on('moveend',()=>{sync(map,devPlanningMap);devPlanningScheduleViewportParcels();devPlanningScheduleViewportZoning();});devPlanningMap.on('moveend',()=>{sync(devPlanningMap,map);devPlanningUpdateLabels();devPlanningScheduleViewportParcels();devPlanningScheduleViewportZoning();});
 devPlanningMap.on('overlayadd overlayremove',()=>{devPlanningUpdateLabels();devPlanningScheduleViewportParcels();devPlanningScheduleViewportZoning();});
 const resize=()=>{const original=map.getContainer(),el=document.getElementById('devPlanningMap');el.style.height=original.clientHeight+'px';el.style.width=original.clientWidth+'px';devPlanningMap.invalidateSize({pan:false});sync(map,devPlanningMap);};
 window.addEventListener('resize',resize);
 if(typeof ResizeObserver!=='undefined'){const observer=new ResizeObserver(resize);observer.observe(map.getContainer());}
 setTimeout(()=>{resize();devPlanningScheduleViewportParcels();devPlanningScheduleViewportZoning();},0);devPlanningRefresh();
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
 #devPlanningParcelStatus{font-size:11px;color:#526174;line-height:1.5;margin:4px 0}.dev-plan-parcel-context-label{pointer-events:none!important;background:transparent;border:0;box-shadow:none;color:#334155;font:11px/18px Arial,sans-serif;white-space:nowrap;text-align:center;text-shadow:0 0 3px #fff,0 0 3px #fff,0 0 3px #fff}#devPlanningStatus,.dev-plan-source{font-size:11px;line-height:1.6;margin:5px 0;color:#64748b}
 .dev-plan-label{font-size:11px;background:#ffffffd9;border:0;box-shadow:none;padding:1px 3px;color:#1e293b;pointer-events:none}
 .dev-plan-north{background:white;padding:5px 8px;font-weight:700;border:1px solid #ccc}
 @media(max-width:1000px){html[data-review-mode="development"] #serviceReviewPage{grid-template-columns:1fr}html[data-review-mode="development"] .service-target-box{position:static}html[data-review-mode="development"] #map{height:360px!important}}
 `;document.head.append(style);
}

// Keep the three result actions visually consistent with the boundary controls.
// This is presentation-only: do not alter button size, status, handlers or RULEs.
function devNormalizeResultActionFonts(){
 if(devMode!=='select')return;
 const id='dev-result-action-font-style';
 if(!document.getElementById(id)){
  const style=document.createElement('style');style.id=id;
  style.textContent='html[data-review-mode="select"] .dev-result-action-font{font-size:14px!important;line-height:1.4!important}';
  document.head.append(style);
 }
 const labels=/^(?:기초현황조사|도시정비검토|도시개발검토|경관성검토|건축시뮬레이션|서울도시정보|도시개발정보)$/;
 document.querySelectorAll('button,a,[role="button"],input[type="button"]').forEach(el=>{
  const text=String(el.tagName==='INPUT'?el.value:el.textContent||'').replace(/\s+/g,'').trim();
  if(el.id==='siteReviewBtn'||el.id==='devNational'||labels.test(text))el.classList.add('dev-result-action-font');
 });
}


/* The first active nationwide service is preliminary survey, not a nationwide
 * development-eligibility engine. Preserve the existing Seoul review handler. */
function devInstallWorkflowActions(){
 const seoul=document.getElementById('siteReviewBtn');
 if(!seoul||document.getElementById('devWorkflow'))return;
 const style=document.createElement('style');
 style.id='devWorkflowStyles';
 style.textContent=`
 #devWorkflow{margin:12px 8px 16px;padding:14px 16px;border:1px solid #dce4ee;border-radius:9px;background:#fff;color:#1e293b}
 #devWorkflow h3{margin:0 0 3px;font-size:15px}
 #devWorkflow p{margin:2px 0 10px;font-size:12px;color:#64748b}
 #devWorkflow .dev-flow-steps{display:flex;gap:10px;align-items:stretch;flex-wrap:wrap}
 #devWorkflow .dev-flow-steps button,#devWorkflow .dev-flow-steps a{padding:11px 16px;border:1px solid #b9c8d9;border-radius:6px;background:#fff;color:#234565;font-size:14px;font-weight:700;cursor:pointer}
 #devWorkflow .dev-flow-steps a{display:inline-flex;align-items:center;justify-content:center;text-decoration:none}
 #devWorkflow .dev-flow-steps #devNational{background:#315eae;border-color:#315eae;color:#fff}
 #devWorkflow .dev-flow-steps button:disabled{background:#e9edf2;border-color:#d3dbe4;color:#8190a0;cursor:not-allowed;opacity:1}
 #devWorkflow .dev-flow-steps #devSeoulReviewAction:not(:disabled){background:#da7838;border-color:#da7838;color:#fff}
 #devWorkflow .dev-flow-steps button:focus-visible,#devWorkflow .dev-flow-steps a:focus-visible{outline:3px solid #2563eb;outline-offset:2px}
 `;
 document.head.append(style);
 const section=document.createElement('section');section.id='devWorkflow';section.setAttribute('aria-label','결과물 작성 및 후속 검토');
 const title=document.createElement('h3');title.textContent='결과물 작성';
 const note=document.createElement('p');note.textContent='① 기초현황조사 → ② 사업방식 검토 → ③ 경관성 검토. 도시개발검토는 준비 중이며 경관성 검토는 별도 경관검토 엔진으로 연결합니다.';
 const actions=document.createElement('div');actions.className='dev-flow-steps';
 const survey=document.createElement('button');survey.type='button';survey.id='devNational';survey.textContent='기초현황조사';survey.disabled=true;survey.onclick=()=>devOpen('development');
 const plans=document.createElement('a');plans.id='devRelatedPlansAction';plans.href='/plans';plans.target='_blank';plans.rel='noopener noreferrer';plans.textContent='상위 및 관련계획 ↗';plans.title='구역계 지정 없이 지역별 계획과 자료를 조회합니다. 새 창에서 열립니다.';
 const seoulAction=document.createElement('button');seoulAction.type='button';seoulAction.id='devSeoulReviewAction';seoulAction.textContent='도시정비검토';seoulAction.disabled=true;seoulAction.title='서울 대상지에 한하여 기존 서울도시정비플랫폼을 실행합니다.';seoulAction.onclick=()=>devOpen('seoul');
 const development=document.createElement('button');development.type='button';development.id='devDevelopmentReview';development.textContent='도시개발검토 · 준비 중';development.disabled=true;development.title='전국 도시개발 사업검토는 아직 구현되지 않았습니다.';
 const landscape=document.createElement('button');landscape.type='button';landscape.id='devLandscapeReview';landscape.textContent='경관성 검토';landscape.disabled=true;landscape.title='확정 대상지를 기준으로 경관성 검토 플랫폼을 엽니다.';
 landscape.onclick=()=>{if(landscape.disabled)return;const w=window.open('https://urban-landscape-review.onrender.com/','_blank');if(w)w.opener=null;else devStatus('팝업이 차단되었습니다. 새 창 열기를 허용하세요.');};
 actions.append(survey,plans,seoulAction,development,landscape);section.append(title,note,actions);
 const placement=seoul.closest('.boundary-review-bar')||seoul.parentElement;
 if(placement)placement.after(section);else document.getElementById('map')?.before(section);
}

function devInstallSurveyNextActions(){
 const parent=document.getElementById('devReview');
 if(!parent||document.getElementById('devSurveyNextActions'))return;
 const section=document.createElement('section');section.id='devSurveyNextActions';section.className='dev-card';
 const title=document.createElement('h3');title.textContent='기초현황조사 이후 단계';
 const note=document.createElement('p');note.style.cssText='margin:10px 14px;color:#526174;font-size:12px';
 note.textContent='기초현황조사 결과를 확인한 뒤 사업방식 검토로 진행합니다. 기존 서울 정비사업 기능만 현재 이용할 수 있습니다.';
 const actions=document.createElement('div');actions.className='dev-actions';actions.style.margin='12px 14px';
 const seoul=document.createElement('button');seoul.type='button';seoul.textContent='서울 도시정비검토 열기';seoul.disabled=devSite?.region!=='SEOUL';seoul.title=seoul.disabled?'서울 지역이 확인된 대상지만 이용 가능합니다.':'기존 서울도시정비 분석을 새 창으로 엽니다.';seoul.onclick=()=>devOpen('seoul');
 const development=document.createElement('button');development.type='button';development.disabled=true;development.textContent='도시개발검토 · 준비 중';
 const landscape=document.createElement('button');landscape.type='button';landscape.textContent='경관성 검토 열기';landscape.onclick=()=>{const w=window.open('https://urban-landscape-review.onrender.com/','_blank');if(w)w.opener=null;};
 actions.append(seoul,development,landscape);section.append(title,note,actions);parent.append(section);
}

function devExport(){const blob=new Blob([JSON.stringify({snapshot:devSite,facts:devFacts,rule_status:'NOT_IMPLEMENTED'},null,2)],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='development-review.json';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);}
async function devInit(){
 const head=document.querySelector('#serviceReviewPage .service-page-head');if(head){head.replaceChildren();const h=document.createElement('h2');h.textContent=devMode==='select'?'대상지 선정 · 전국 지도':devMode==='seoul'?'서울 도시정비검토':'기초현황조사';head.append(h);if(devMode==='select')head.hidden=true;}
 const status=document.createElement('p');status.id='devStatus';status.setAttribute('role','status');status.style.cssText='padding:12px;background:#edf6fa;color:#143852';head?.after(status);
 if(devMode!=='seoul'){const h=document.querySelector('.cc-header h1');if(h)h.textContent='도시계획 기초현황조사·사업검토 플랫폼';const sub=document.querySelector('.cc-header .sub');if(sub)sub.textContent='대상지 선정';document.querySelectorAll('.main-map-layer-note').forEach(e=>e.textContent='전국 VWorld 용도지역·지적·시설 표시용 도면. 확대하여 확인하세요. 자료 미확보는 시설 없음이 아닙니다.');}
 if(devMode==='select'){
 devEnsureBaseMap();
 devInstallParcelSelection();
 devInstallParcelView();
 devInstallWorkflowActions();devNormalizeResultActionFonts();
 const input=document.getElementById('boundaryAddressInput');if(input)input.placeholder='정식 시·도명부터 지번까지 · 여러 주소는 줄바꿈';devStatus('');setInterval(devRefresh,600);devRefresh();return;
 }
 try{
 const token=new URLSearchParams(location.search).get('site');if(!token)throw Error('초기 화면에서 대상지를 확정한 뒤 검토창을 여세요.');devSite=await devApi('snapshots/'+encodeURIComponent(token));devSite.id=token;if(devMode==='seoul'&&devSite.region!=='SEOUL')throw Error('서울 포함 여부가 확인되지 않아 서울 사업검토를 실행할 수 없습니다.');
 activeGeometry=JSON.parse(JSON.stringify(devSite.geometry));devSignature=JSON.stringify(activeGeometry);boundaryReferenceGeometry=JSON.parse(JSON.stringify(activeGeometry));drawnItems.clearLayers();L.geoJSON(activeGeometry,{style:{color:'#dc2626',weight:4,fillOpacity:.04}}).eachLayer(l=>drawnItems.addLayer(l));setBoundaryReferenceMetrics();fitToActiveArea();
 devStatus(`확정 대상지 사본 · ${devSite.area_m2.toLocaleString()}㎡ · 원래 창을 수정해도 이 창의 대상지는 유지됩니다.`);
 if(devMode==='seoul'){await devRunOriginal();return;}
 devInstallLayout();
 devInstallPlanningMap();
 await devPlanningLoadSymbolCatalog();
 const section=document.createElement('section');section.id='devReview';section.innerHTML=`
 <h2>기초현황조사</h2><p>확정된 대상지의 전국 도시계획·토지·건축물·자연환경 기초현황을 수집합니다. 도시개발사업 적합성은 아직 판정하지 않습니다.</p>
 <div class="dev-actions"><button id="devFetch">자료 다시 조회</button><button id="devExport">조회결과 저장</button></div>
 <div id="devConnectionSummary" role="status" class="dev-alert">자료 연결 확인 중</div>
 <section class="dev-card"><h3>지적 현황</h3><div id="devFacts-parcel"></div></section>
 <section class="dev-card"><h3>용도지역</h3><div id="devFacts-zoning"></div></section>
 <section class="dev-card"><h3>용도지구</h3><div id="devFacts-district"></div></section>
 <section class="dev-card"><h3>용도구역</h3><div id="devFacts-area"></div></section>
 <section class="dev-card"><h3>도시계획시설</h3><div id="devFacts-facility"></div></section>
 <div class="dev-scope"><b>검토 범위</b><p>전국 대상지의 공공자료를 조회하여 공간현황·통계·조서를 제공합니다.</p><p>자료 미확보는 시설 없음 또는 사업 부적합을 뜻하지 않습니다.</p><p><a href="/api/development/admin/diagnostics" target="_blank" rel="noopener">관리자 연결상태 확인</a> · <a href="https://www.eum.go.kr" target="_blank" rel="noopener">토지이음</a></p></div>`;
 document.getElementById('serviceReviewPage').append(section);document.getElementById('devFetch').onclick=()=>devLoadFacts(true);document.getElementById('devExport').onclick=devExport;
 const targetTitle=document.querySelector('.service-target-box .service-block-title b');if(targetTitle)targetTitle.textContent='확정 대상지';const targetNote=document.querySelector('.service-target-box .service-block-title span');if(targetNote)targetNote.textContent='이 창에서는 구역계를 수정하지 않습니다.';
 devInstallInventory();devInstallSurveyNextActions();setTimeout(()=>map.invalidateSize(false),0);await devLoadFacts();
 }catch(e){devSite=null;devStatus(e.message);}
}

// V5 inventories: all collection, clipping, deduplication and XLSX creation are server-side.
let devInventoryBusy=false;
function devInstallInventory(){
 const box=document.createElement('section');box.id='devInventory';box.className='dev-card';
 box.innerHTML='<h3>도시계획 현황 분석보고서</h3><div style="padding:14px"><p>확정 구역계에 편입되는 필지를 기준으로 공공 대장을 조회합니다. 건물별 편입 여부는 별도 확인이 필요합니다.</p><div class="dev-actions"><button id="devInventoryLoad">기초현황 자료 조회 / 미확보 재조회</button><button id="devInventoryDownload" disabled>엑셀 다운로드</button></div><p id="devInventoryStatus" role="status">미조회 · 44개 항목의 분석 완료를 의미하지 않습니다.</p><div id="devInventoryResults"></div></div>';
 document.getElementById('devReview').append(box);
 document.getElementById('devInventoryLoad').onclick=devLoadInventory;
 document.getElementById('devInventoryDownload').onclick=async()=>{
  const status=document.getElementById('devInventoryStatus');
  try{const res=await fetch('/api/development/reports/'+encodeURIComponent(devSite.id)+'/xlsx');if(!res.ok)throw Error('다운로드 실패 · 대상지 만료 또는 서버 응답을 확인하세요.');const url=URL.createObjectURL(await res.blob());const a=document.createElement('a');a.href=url;a.download='토지건축물_현황조서.xlsx';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(e){status.textContent=e.message;}
 };
 devApi('reports/'+encodeURIComponent(devSite.id)).then(d=>{devRenderInventory(d);devRenderSourceDetails(d);}).catch(()=>{});
}
function devRenderInventory(data){
 const root=document.getElementById('devInventoryResults');root.replaceChildren();
 const note=document.createElement('p');note.textContent=data.parcel_status==='REFERENCE'?`보조 지적 ${data.land.length}필지 참고목록 · ${data.parcel_note||'원문 진위·전체 수록 여부 미검증'}`:data.parcel_status==='UNAVAILABLE'?'연속지적 미확보 · 토지·건축물 조서 보류. 확보된 공간자료는 아래에서 확인하세요.':`${data.land.length}필지 · 관련 건축물대장 ${data.buildings.length}건 · 기준 ${data.as_of} · 빈칸은 미확보입니다.`;root.append(note);
 const buildingFact=data.building_fact||{},bc=buildingFact.counts||{};
 if(buildingFact.geometry_status){
  const box=document.createElement('div');
  box.style.cssText='margin:8px 0;padding:9px 12px;border:1px solid #cbd5e1;background:#f8fafc;font-size:12px;line-height:1.7';
  box.textContent='건축물 통합FACT ['+buildingFact.geometry_status+'] · 공간도형 '+(bc.mapped_shapes??'미확보')+
    '개 · 대장 직접연결 '+(bc.direct_register_matches??0)+'개 · 필지공간 잠정연계 '+(bc.spatial_register_candidates??0)+'개 · GIS 속성만 '+(bc.gis_attribute_only??0)+
    '개 · 속성불일치 '+(bc.conflicting_shapes??0)+'개 · 도형 미연계 대장 '+(bc.register_without_direct_geometry??0)+
    '건. 실제 건축물 법정 동수와 같다고 단정하지 않음.';
  root.append(box);
 }
 function table(title,columns,rows){const details=document.createElement('details');const summary=document.createElement('summary');summary.textContent=title;details.append(summary);const wrap=document.createElement('div');wrap.style.cssText='overflow:auto;max-height:460px';const t=document.createElement('table');t.style.cssText='min-width:650px;width:100%;font-size:12px;border-collapse:collapse';const thead=t.createTHead();const header=thead.insertRow();columns.forEach(([key,label])=>{const th=document.createElement('th');th.textContent=label;th.style.cssText='padding:8px;text-align:left;background:#eaf0f5';header.append(th);});const body=t.createTBody();rows.forEach(r=>{const tr=body.insertRow();columns.forEach(([key])=>{const td=tr.insertCell();td.textContent=r[key]??'미확보';td.style.cssText='padding:8px;border-bottom:1px solid #ddd';});});wrap.append(t);details.append(wrap);root.append(details);}
 table('토지조서 (화면 최대 100필지 · 엑셀 전체)',[['jibun','지번'],['parcel_assurance','지적 확인수준'],['category','지목'],['official_area','공부㎡'],['overlap_area','편입 도형㎡'],['ownership','소유구분'],['zoning','중첩 용도지역'],['price','지가 원/㎡'],['ledger_status','대장 상태']],data.land.slice(0,100));
 table('건축물조서 (화면 최대 100건 · 엑셀 전체)',[['address','소재지'],['use','용도'],['structure','구조'],['floors','층수'],['site_area','대지면적㎡'],['footprint','건축면적㎡'],['floor_area','연면적㎡'],['coverage','건폐율%'],['coverage_basis','건폐율 근거'],['far','용적률%'],['far_basis','용적률 근거'],['age','경과년'],['scope','편입확인']],data.buildings.slice(0,100));
 table('도시계획 현황 분석보고서 · '+data.items.length+'개 검토항목', [['id','번호'],['group','분류'],['name','항목'],['status','자료 상태'],['detail','내용·한계']],data.items);
 const failures=(data.source_jobs||[]).filter(v=>v.error_code);if(failures.length)table('자료 연결 결과', [['id','조회 항목'],['status','상태'],['detail','내용'],['error_code','오류구분']],failures);
 document.getElementById('devInventoryDownload').disabled=false;
}
async function devLoadInventory(){
 if(devInventoryBusy)return;if(devBusy){document.getElementById('devInventoryStatus').textContent='공간자료 조회가 끝난 뒤 조서를 조회하세요.';return;}devInventoryBusy=true;const button=document.getElementById('devInventoryLoad'),status=document.getElementById('devInventoryStatus');button.disabled=true;document.getElementById('devInventoryDownload').disabled=true;
 const base='reports/'+encodeURIComponent(devSite.id);let failed=0;
 try{
  status.textContent='확정 구역계의 지적자료 확인 중';await devSyncStoredFacts();
  if(!devHasFact(devFacts.find(v=>v.layer==='LP_PA_CBND_BUBUN'))){await devFetchNationalLayer('LP_PA_CBND_BUBUN',devSite.geometry,{});await devSyncStoredFacts();}
  const data=await devApi(base+'/prepare',{});let i=0;
  for(const row of data.land){i++;status.textContent=`토지·건축물대장 조회 ${i}/${data.land.length}필지 · 창을 유지하세요.`;
   try{await devApi(base+'/parcel/'+encodeURIComponent(row.pnu),{});}catch(e){failed++;if(/429|조회 중|사용 중/.test(e.message)){throw Error('조서 조회가 사용 중입니다. 잠시 후 미확보 재조회를 누르세요.');}}
  }
  const jobs=await devApi(base+'/source-jobs');let ji=0;
  for(const job of jobs.jobs){ji++;status.textContent=`공공자료 연결 ${ji}/${jobs.jobs.length} · ${job.label}`;try{const r=await devApi(base+'/source-jobs/'+encodeURIComponent(job.id),{});if(r.status==='ERROR')failed++;if(r.error_code==='RATE_LIMIT')throw Error('호출 한도 도달 · 잠시 후 다시 조회하세요.');}catch(e){if(/호출 한도|조회 중/.test(e.message))throw e;failed++;}}
  const result=await devApi(base);await devSyncStoredFacts();devRenderInventory(result);devRenderSourceDetails(result);const missing=result.land.filter(r=>!!r.missing_fields||r.ledger_status!=='확보'||r.price_status!=='확보'||!['확보','대장 응답 0건'].includes(r.building_status)).length;
  const sourceJobs=result.source_jobs||[];
  const unconfigured=sourceJobs.filter(j=>['MISSING_SOURCE','MISSING_CREDENTIAL'].includes(j.status)).length;
  const unavailable=sourceJobs.filter(j=>j.status==='OUTSIDE_COVERAGE').length;
  const parcelMessage=result.parcel_status==='REFERENCE'?`보조 지적 ${result.land.length}필지 참고목록 · 전체 필지 수록 여부 미확인`:result.parcel_status==='UNAVAILABLE'?'연속지적 미확보 · 필지 수 미확인 · 토지·건축물 조서 보류':`${result.land.length}필지 · 일부 속성 미확보 ${missing}필지`;
  status.textContent=`조회 처리 종료 · ${parcelMessage}. 요청 실패 ${failed}건 · 원자료/인증/관측소 설정 필요 ${unconfigured}건 · 자료 수록범위 밖 ${unavailable}건. 건수는 조회 작업 기준이며 전체 검토항목 수와 다릅니다. 항목별 확보 상태는 아래 표에서 확인하세요.`;
 }catch(e){status.textContent=e.message;try{const d=await devApi(base);devRenderInventory(d);devRenderSourceDetails(d);}catch(_){}}
 finally{devInventoryBusy=false;button.disabled=false;}
}

let devSourceDetailMap=null;
function devRenderSourceDetails(data){
 if(devSourceDetailMap){devSourceDetailMap.remove();devSourceDetailMap=null;}
 document.getElementById('devSourceDetail')?.remove();
 const section=document.createElement('section');section.id='devSourceDetail';section.className='dev-card';
 const heading=document.createElement('h3');heading.textContent=data.items.length+'개 항목별 자료·통계·도면';section.append(heading);
 const select=document.createElement('select');select.style.cssText='margin:12px;max-width:90%;padding:8px';
 for(const item of data.items){const option=document.createElement('option');option.value=item.id;option.textContent=`${item.id} ${item.name} · ${item.status}`;select.append(option);}section.append(select);
 const info=document.createElement('p');info.style.padding='12px';section.append(info);
 const surveyTable=document.createElement('div');
 surveyTable.style.cssText='display:none;margin:0 0 14px;overflow-x:auto';
 section.append(surveyTable);
 const cadActions=document.createElement('div');cadActions.className='dev-actions';
 const cadPrepare=document.createElement('button');cadPrepare.type='button';cadPrepare.textContent='표준 도면 배경자료 준비';
 const cadSingle=document.createElement('button');cadSingle.type='button';cadSingle.textContent='선택 항목 DXF 다운로드';
 const cadBulk=document.createElement('button');cadBulk.type='button';cadBulk.textContent='공간도면 일괄 ZIP 다운로드';
 const cadStatus=document.createElement('span');cadStatus.style.cssText='font-size:12px;color:#526174;align-self:center';
 cadActions.append(cadPrepare,cadSingle,cadBulk,cadStatus);section.append(cadActions);
 let cadReady=false,cadPreparing=null,cadAutoAttempted=false;
 async function prepareCadBackground(){
  if(cadReady)return;
  if(cadPreparing)return cadPreparing;
  cadPrepare.disabled=true;
  cadPreparing=(async()=>{
   const base='reports/'+encodeURIComponent(devSite.id)+'/cad-background';
   let state=await devApi(base+'/status');
   for(let step=0;!state.ready&&step<64;step++){
    cadStatus.textContent='주변 지적 수집 '+state.done+'/'+state.total+'구역 · 서버 요청 '+state.request_count+'회 (완료된 구역 저장)';
    state=await devApi(base+'/next',{});
   }
   if(!state.ready)throw Error('주변 지적 수집 요청 상한 도달 · 자료 완전성을 확인해야 합니다.');
   cadReady=true;
   cadStatus.textContent='표준 도면 배경자료 준비 완료 · 실제 좌표 기반';
   paint(); // Refresh the preview now that the same verified CAD context exists.
  })().catch(e=>{
   cadStatus.textContent='도면 배경자료 미완료: '+(e.message||'서버 오류')+' · 수집된 구역은 유지됩니다.';
   throw e;
  }).finally(()=>{cadPreparing=null;cadPrepare.disabled=false;});
  return cadPreparing;
 }
 cadPrepare.onclick=()=>prepareCadBackground().catch(()=>{});

 async function downloadCad(file,url){
  cadSingle.disabled=true;cadBulk.disabled=true;cadStatus.textContent='주변 지적자료 준비 확인 중…';
  try{
   await prepareCadBackground();
   cadStatus.textContent='실좌표 DXF 파일 생성 및 전송 중…';
   const response=await fetch(url,{cache:'no-store'});
   if(!response.ok){
    let detail='CAD 생성 실패 ('+response.status+')';
    try{const payload=await response.json();if(typeof payload.detail==='string')detail=payload.detail;}catch(_){}
    if(response.status===503&&detail==='CAD 생성 실패 (503)')detail='서버 503: 배포 서버 또는 제공기관 연결 확인 필요. Cloudtype 로그에서 실패 단계를 확인하세요.';
    throw Error(detail);
   }
   const blob=await response.blob(),objectUrl=URL.createObjectURL(blob),a=document.createElement('a');
   a.href=objectUrl;a.download=file;document.body.append(a);a.click();a.remove();
   setTimeout(()=>URL.revokeObjectURL(objectUrl),2000);
   cadStatus.textContent='다운로드 파일 생성 완료 · CAD에서 도곽과 지적을 확인하세요.';
  }catch(e){cadStatus.textContent=e.message||'CAD 다운로드 실패';}
  finally{cadBulk.disabled=false;cadSingle.disabled=!cadSingle.dataset.available;}
 }
 cadSingle.onclick=()=>downloadCad('survey_'+select.value.replace('-','_')+'.dxf','/api/development/reports/'+encodeURIComponent(devSite.id)+'/dxf/'+encodeURIComponent(select.value));
 cadBulk.onclick=()=>downloadCad('survey_cad_bundle.zip','/api/development/reports/'+encodeURIComponent(devSite.id)+'/dxf.zip');
 const sheetPreview=document.createElement('img');
 sheetPreview.alt='현황도 표준 도곽 미리보기';
 sheetPreview.style.cssText='display:none;width:100%;height:auto;border:1px solid #cbd5e1;background:#fff;object-fit:contain';
 section.append(sheetPreview);
 const mapbox=document.createElement('div');mapbox.id='devSourceMap';mapbox.style.height='360px';section.append(mapbox);
 const legendbox=document.createElement('div');legendbox.style.cssText='display:flex;flex-wrap:wrap;gap:8px;padding:8px 12px;font-size:12px';section.append(legendbox);
 const stats=document.createElement('pre');stats.style.cssText='white-space:pre-wrap;max-height:360px;overflow:auto;font-size:12px;padding:12px';section.append(stats);
 const raw=document.createElement('details'),summary=document.createElement('summary'),rawtext=document.createElement('pre');summary.textContent='필지별 규제 관계 원자료';rawtext.style.cssText='white-space:pre-wrap;max-height:240px;overflow:auto';raw.append(summary,rawtext);section.append(raw);
 document.getElementById('devReview').append(section);
 // Reuse the main map's visible base tiles; do not introduce a new map API or key.
 devSourceDetailMap=L.map(mapbox,{scrollWheelZoom:false,crs:map.options.crs,zoomSnap:map.options.zoomSnap,minZoom:map.getMinZoom(),maxZoom:map.getMaxZoom()});
 const visibleBases=[];
 map.eachLayer(layer=>{
  if(layer instanceof L.TileLayer && map.hasLayer(layer) && (layer.options.pane||'tilePane')==='tilePane' && typeof layer._url==='string')visibleBases.push(layer);
 });
 const base=visibleBases[0];
 if(base){
  const baseOpts={...base.options};
  (base instanceof L.TileLayer.WMS?L.tileLayer.wms(base._url,{...baseOpts,...base.wmsParams}):L.tileLayer(base._url,baseOpts)).addTo(devSourceDetailMap);
 }
 // The cadastral outline remains visible when switching report items.
 const parcels=L.layerGroup().addTo(devSourceDetailMap),drawn=L.layerGroup().addTo(devSourceDetailMap);
 const parcelFact=devFacts.find(v=>v.layer==='LP_PA_CBND_BUBUN'&&devHasFact(v));
 if(parcelFact?.features?.length){
  L.geoJSON(parcelFact.features,{style:{color:'#475569',weight:1,opacity:.85,fill:false},onEachFeature:(f,l)=>{
   const p=f.properties||{},jibun=p.jibun||p.pnu;
   if(jibun){const label=document.createElement('span');label.textContent=String(jibun);l.bindTooltip(label);}
  }}).addTo(parcels);
 }
 L.control.layers(null,{'연속지적':parcels,'선택 항목':drawn},{collapsed:true}).addTo(devSourceDetailMap);
 function renderSurveyTable(item){
  surveyTable.replaceChildren();
  const t=item.survey_table;
  if(!/^(?:2-[1-6]|3-[1-7])$/.test(item.id)){surveyTable.style.display='none';return;}
  surveyTable.style.display='block';
  const title=document.createElement('div');
  title.textContent=t?.title||('표 '+item.id+(item.id.startsWith('3-')?' 건축물현황':' 토지현황'));
  title.style.cssText='font-size:13px;font-weight:700;color:#243e7a;margin:0 0 6px';
  surveyTable.append(title);
  if(!t?.rows?.length){
   const empty=document.createElement('div');
   empty.textContent='현황표 미확보 · 분류별 공간도형과 면적 자료 확인 필요 (0㎡로 간주하지 않음)';
   empty.style.cssText='padding:12px;border:1px solid #d9e2ed;background:#f8fafc;font-size:12px';
   surveyTable.append(empty);
   return;
  }
  const tab=document.createElement('table');
  tab.style.cssText='width:100%;border-collapse:collapse;font-size:12px;min-width:510px;font-variant-numeric:tabular-nums';
  const header=document.createElement('thead'),hrow=document.createElement('tr');
  for(const text of (item.id.startsWith('3-')?['구분','건축물조서(건)','구성비(%)','비고']:['구분','면적(㎡)','구성비(%)',t.count_header||'필지수(필지)','비고'])){
   const th=document.createElement('th');th.textContent=text;
   th.style.cssText='padding:7px 5px;border:1px solid #9ca9bf;background:#dfe7f6;color:#21376d;text-align:center';
   hrow.append(th);
  }
  header.append(hrow);tab.append(header);
  const body=document.createElement('tbody');
  for(const row of t.rows){
   const tr=document.createElement('tr');
   const cells=item.id.startsWith('3-')?[row.label,row.count,row.pct,row.note||'']:[row.label,row.area_m2,row.pct,row.count,row.note||''];
   for(let i=0;i<cells.length;i++){
    const td=document.createElement('td');
    const v=cells[i];
    td.textContent=v===null||v===undefined?'–':i===1&&typeof v==='number'?v.toLocaleString('ko-KR',{minimumFractionDigits:1,maximumFractionDigits:2}):i===2&&typeof v==='number'?v.toFixed(1):String(v);
    td.style.cssText='border-bottom:1px '+(row.kind==='category'?'dotted':'solid')+' #9ca9bf;padding:5px 8px;text-align:'+(i===1||i===2?'right':'left');
    if(row.kind==='total')td.style.fontWeight='700';
    if(row.kind==='boundary')td.style.background='#f4f6fb';
    tr.append(td);
   }
   body.append(tr);
  }
  tab.append(body);surveyTable.append(tab);
  const note=document.createElement('div');
  note.style.cssText='font-size:11px;color:#526174;line-height:1.5;padding:6px 2px';
  note.textContent=(t.status==='COMPLETE'?'확인된 공간자료 기준':'잠정·부분집계 · 원자료 검증 필요')+' · '+(t.notes||[]).join(' · ')+
   ' · 자료출처: '+(t.source||item.source||'미확보')+
   (t.as_of?' · 자료기준일: '+String(t.as_of).slice(0,10):'')+
   (item.id.startsWith('3-')?' · 건폐율·용적률의 플랫폼 내부산정값은 법정 확정값 아님. 대장과 도형의 연결 후보는 검증 필요.':'');
  surveyTable.append(note);
 }
 function paint(){
  const item=data.items.find(x=>x.id===select.value);drawn.clearLayers();legendbox.replaceChildren();
  renderSurveyTable(item);
  sheetPreview.style.display='none';sheetPreview.removeAttribute('src');mapbox.style.display='block';legendbox.style.display='flex';
  if(!cadReady&&!cadAutoAttempted&&item.id==='1-1'){
   cadAutoAttempted=true;prepareCadBackground().catch(()=>{});
  }
  const available=item.id==='1-1'||(item.thematic_type==='building'&&item.cad_exportable===true)||(item.id==='2-6' ? item.thematic_type==='landcover'&&item.provider_verified===true : ((item.provider_verified===true||item.verified_feature_sources===true)&&!!item.features?.length&&['공간자료 확보','일부 확보'].includes(item.status))||(['공식 파일자료 확보','일부 확보'].includes(item.status)&&!!item.features?.length&&item.source!=='VWorld'&&item.thematic_type!=='ecobank'));
  const usable=available;
  cadSingle.dataset.available=usable?'yes':'';
  cadSingle.disabled=!usable;cadSingle.title=usable?'확보된 공간도형 기준 CAD 출력':'검증된 공간도형이 없는 항목은 CAD 출력 불가';
  if(!cadPreparing)cadStatus.textContent=usable?(cadReady?'표준 도면 배경자료 준비 완료':'표준 도면 배경자료 준비 필요 · 준비 버튼을 누르거나 자동 조회를 확인하세요'):'이 항목은 CAD 도형 미확보 · 일괄 ZIP 목록에 미확보로 기록';
  if(available&&cadReady&&['1-1','2-1','2-2','2-3','2-4','2-5','2-6','5-2','5-3','5-4','5-5','5-6','5-7','5-8','4-3','4-1','3-1','3-2','3-3','3-4','3-5','3-6','3-7'].includes(item.id)){
   const selected=item.id;
   sheetPreview.onload=()=>{
    if(select.value!==selected)return;
    sheetPreview.style.display='block';mapbox.style.display='none';legendbox.style.display='none';
   };
   sheetPreview.onerror=()=>{
    if(select.value!==selected)return;
    sheetPreview.style.display='none';mapbox.style.display='block';legendbox.style.display='flex';
    cadStatus.textContent+=' · 표준 인쇄도면 미리보기는 미확보(기존 공간지도로 표시)';
    setTimeout(()=>devSourceDetailMap?.invalidateSize(false),0);
   };
   sheetPreview.src='/api/development/reports/'+encodeURIComponent(devSite.id)+'/sheet/'+encodeURIComponent(item.id)+'.svg';
  }
  if(item.id==='2-6' && !available && item.landcover_map_available){
   // WMS reference preview is independent of CAD background or vector area.
   // The API clips the original raster to the site and prints a reference-only
   // border, right-hand panel, and verified provenance; DXF remains disabled.
   const selected=item.id;
   sheetPreview.onload=()=>{
    if(select.value!==selected)return;
    sheetPreview.style.display='block';mapbox.style.display='none';legendbox.style.display='none';
    cadStatus.textContent='사업구역 경계로 잘라낸 토지피복 WMS 참고도 · 수치·편집 DXF 미확보';
   };
   sheetPreview.onerror=()=>{
    if(select.value!==selected)return;
    sheetPreview.style.display='none';mapbox.style.display='block';legendbox.style.display='flex';
    cadStatus.textContent='WMS 참고도 출력 실패 · 경계로 잘라낸 공간지도를 표시합니다';
    setTimeout(()=>devSourceDetailMap?.invalidateSize(false),0);
   };
   sheetPreview.src='/api/development/reports/'+encodeURIComponent(devSite.id)+'/sheet/2-6.svg';
  }
  info.textContent=`${item.detail} · 출처: ${item.source||'미확보'}${item.as_of?' · 자료 기준일: '+item.as_of:''}`;
  const boundary=L.geoJSON(devSite.geometry,{style:{color:'#dc2626',weight:3,fill:false}}).addTo(drawn);devSourceDetailMap.fitBounds(boundary.getBounds(),{padding:[12,12]});
  // Defense in depth: older report snapshots cannot shade cadastral parcels as buildings.
  const drawableFeatures=(item.features||[]).filter(f=>item.thematic_type!=='building'||
   (f.properties?.geometry_basis==='BUILDING_FOOTPRINT'&&f.properties?.geometry_source==='LT_C_BLDGINFO'));
  if(drawableFeatures.length)L.geoJSON(drawableFeatures,{
   style:f=>({color:['ownership','jimok','land_numeric','land_shape','landcover','ecobank','building','elevation','slope_tin'].includes(item.thematic_type)?'#526174':'#126d88',
    weight:['ownership','jimok','land_numeric','land_shape','landcover','ecobank','building','elevation','slope_tin'].includes(item.thematic_type)?0.8:1.5,
    fillColor:['ownership','jimok','land_numeric','land_shape','landcover','ecobank','building','elevation','slope_tin'].includes(item.thematic_type)?(f.properties?.color||'#cccccc'):'#126d88',
    fillOpacity:['ownership','jimok','land_numeric','land_shape','landcover','ecobank','building','elevation','slope_tin'].includes(item.thematic_type)?0.68:0.2}),
   onEachFeature:(f,l)=>{
    const label=document.createElement('span');label.textContent=f.properties?.name||'원자료';l.bindTooltip(label);
    if(item.thematic_type==='building'){
     const status=f.properties?.join_status;
     const assurance=status==='EXACT_ID'?'대장 관리번호 직접일치':status==='PARCEL_SPATIAL_REFERENCE'?'대장 PNU 잠정연계 · 실제 건물 동일성 미확정':'대장 미연계 · GIS 원천속성만 참조';
     l.bindPopup('VWorld GIS건물통합정보 건물 외곽선 · '+assurance);
    }
   }
  }).addTo(drawn);
  if(Array.isArray(item.legend))for(const entry of item.legend){
   const row=document.createElement('span');row.style.cssText='display:inline-flex;align-items:center;gap:5px';
   const chip=document.createElement('span');chip.style.cssText='display:inline-block;width:14px;height:14px;border:1px solid #64748b;background-color:'+entry.color;
   const title=document.createElement('span');title.textContent=item.thematic_type==='building'
      ?entry.name+' ('+entry.count+'개 GIS 건물외곽선)'
      :['elevation','slope_tin'].includes(item.thematic_type)
        ?entry.name+' ('+entry.area_m2.toLocaleString()+'㎡)'
        :entry.name+' ('+entry.count+'필지 · '+entry.area_m2.toLocaleString()+'㎡)';
   row.append(chip,title);legendbox.append(row);
  }
  if(item.id==='2-6'&&item.landcover_wms&&!item.features?.length){
   // Use the backend-validated official WMS PNG, not unverified client-side
   // tiles. The image is WMS-only and cannot enter CAD or area statistics.
   const sourceLabel=document.createElement('div');
   sourceLabel.style.cssText='font-size:12px;padding:7px;background:#fff4d6;color:#694900';
   sourceLabel.textContent=item.landcover_map_available
      ?'환경부 2025년 세분류 토지피복 WMS 원영상 확보 · 면적·CAD 원도형 미확보'
      :'토지피복 WMS 영상 미확보 · 제공기관 수신 상태를 확인하세요. 면적·CAD 불가';
   legendbox.append(sourceLabel);
   if(item.landcover_map_available&&item.landcover_map_bounds?.length===2){
    const img='/api/development/reports/'+encodeURIComponent(devSite.id)+'/landcover-clipped.png';
    const cover=L.imageOverlay(img,item.landcover_map_bounds,{opacity:0.9,interactive:false});
    cover.on('error',()=>{if(select.value==='2-6')cadStatus.textContent='2-6 토지피복 WMS 이미지 다운로드 실패 · 서버 로그 확인';});
    cover.addTo(drawn);
    boundary.bringToFront();
   }
  }
  if(item.map_available){const url='/api/development/reports/'+encodeURIComponent(devSite.id)+'/ecvam.png';L.imageOverlay(url,boundary.getBounds(),{opacity:.65}).addTo(drawn);boundary.bringToFront();}
  stats.textContent=item.statistics?JSON.stringify(item.statistics,null,2):'수치 분석자료 미확보 · 도면만 확보된 경우 면적을 계산하지 않습니다.';
  rawtext.textContent=item.regulation_rows?.length?JSON.stringify(item.regulation_rows,null,2):'규제 참고행 없음 또는 미조회. 비해당 확정 근거가 아닙니다.';
  setTimeout(()=>devSourceDetailMap?.invalidateSize(false),0);
 }
 select.onchange=paint;paint();
}
