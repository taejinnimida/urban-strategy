/* Public renderer: published appearance only. No drafts, auth tokens or editor code. */
(()=>{'use strict';
 const blank=()=>({schema:1,elements:{},maps:{}}),groups=['spatial-module','spatial-module-title','map-legend-chip','mini-medical-legend','activation-table','activation-section','scheme-modal-note','scheme-tag','cc-header-action','r101-view-toolbar'];
 let current=blank(),published=blank(),preview=false,scheduled=0,popup='default';
 const sheet=document.createElement('style');sheet.id='ui-published-style';document.head.appendChild(sheet);
 const hash=s=>{let h=2166136261;for(const c of s){h^=c.charCodeAt(0);h=Math.imul(h,16777619);}return (h>>>0).toString(36);};
 const excluded=e=>!e||e.closest('#ui-editor-panel,#ui-editor-toggle,.admin-only,.admin-login,script,style,svg,canvas,.leaflet-pane')||['HTML','BODY','HEAD'].includes(e.tagName);
 function key(e){
  if(excluded(e))return null;
  const pop=e.closest('#schemeDetailPopupBody');
  if(e.id&&!pop)return 'element:'+e.id.replace(/[^\w.:-]/g,'_');
  const root=pop||e.closest('[id]')||document.body,parts=[];let node=e;
  while(node&&node!==root){let token=node.tagName.toLowerCase();
   if(node.matches('tr'))token+='-'+hash(node.querySelector('th,td')?.textContent.trim()||'row');
   else if(node.matches('section'))token+='-'+hash(node.querySelector('h3,h4,h5')?.textContent.trim()||'section');
   else {const siblings=[...(node.parentElement?.children||[])].filter(x=>x.tagName===node.tagName);token+='-'+siblings.indexOf(node);}
   parts.unshift(token);node=node.parentElement;
  }
  return (pop?'popup:'+popup:'element:'+(root.id||'page'))+':'+hash(parts.join('/'));
 }
 function annotate(){
  for(const e of document.querySelectorAll('div,section,article,h1,h2,h3,h4,h5,p,span,b,i,em,strong,small,table,thead,tbody,tr,th,td,button,a,label,ul,li')){
   if(excluded(e))continue;const k=key(e);if(k)e.dataset.uiKey=k;
   const group=groups.find(g=>e.classList.contains(g))||(e.matches('button')?'button':e.matches('h3,h4,h5')?'heading':e.matches('th,td')?'table-cell':null);
   if(group)e.dataset.uiGroup=group;
  }
 }
 function mapList(){try{return typeof miniMapViewSpecs==='function'?miniMapViewSpecs().map(x=>x.map):[];}catch{return [];}}
 const tracked=new WeakMap(),bound=new WeakSet();
 function mapRole(layer){const p=String(layer.options?.pane||'').toLowerCase(),f=layer.feature?.properties||{};if(/site/.test(p)||f.display_only)return 'site';if(/background|base|parcel/.test(p)||f._view_road)return 'background';return 'theme';}
 function overrides(mp,layer){return current.maps?.[mp.getContainer().id+':'+mapRole(layer)]||{};}
 function pathStyles(value){const out={};for(const k of ['color','fillColor','weight','opacity','fillOpacity'])if(value[k]!=null)out[k]=value[k];return out;}
 function visit(mp,layer){
  if(layer.eachLayer){layer.eachLayer(l=>visit(mp,l));return;}
  if(typeof layer.setStyle!=='function')return;
  let record=tracked.get(layer);if(!record){record={original:layer.setStyle,base:pathStyles(layer.options||{})};tracked.set(layer,record);layer.setStyle=function(styles){Object.assign(record.base,pathStyles(styles||{}));return record.original.call(this,{...styles,...pathStyles(overrides(mp,this))});};}
  record.original.call(layer,{...record.base,...pathStyles(overrides(mp,layer))});
 }
 function maps(){for(const mp of mapList()){if(!bound.has(mp)){bound.add(mp);mp.on('layeradd',e=>visit(mp,e.layer));}mp.eachLayer(l=>visit(mp,l));}}
 function css(){const out=[];for(const [k,values] of Object.entries(current.elements||{})){if(!/^(element|common|popup):[\w.:-]{1,180}$/.test(k))continue;const selector=k.startsWith('common:')?'[data-ui-group="'+CSS.escape(k.slice(7))+'"]':'[data-ui-key="'+CSS.escape(k)+'"]';const props=Object.entries(values).filter(([p,v])=>/^[a-z-]+$/.test(p)&&typeof v==='string'&&!/[{};<>]/.test(v)&&CSS.supports(p,v)).map(([p,v])=>p+':'+v+'!important').join(';');out.push(selector+'{'+props+'}');const symbol=Object.entries(values).filter(([p,v])=>['legend-symbol-width','legend-symbol-height'].includes(p)&&/^\d+(?:\.\d+)?(px|em|rem|%)$/.test(v)).map(([p,v])=>p.replace('legend-symbol-','')+':'+v+'!important').join(';');if(symbol)out.push(selector+'::before,'+selector+' > b{'+symbol+';display:inline-block}');}
  for(const [k,v] of Object.entries(current.maps||{})){if(v.labelSize!=null){const id=k.split(':')[0];out.push('#'+CSS.escape(id)+' .mini-context-label-anchor span,#'+CSS.escape(id)+' .leaflet-tooltip{font-size:'+Number(v.labelSize)+'px!important}');}}
  sheet.textContent=out.join('\n');
 }
 function refresh(){annotate();css();maps();}
 function schedule(){if(scheduled)return;scheduled=setTimeout(()=>{scheduled=0;refresh();},80);}
 new MutationObserver(rows=>{if(rows.some(r=>r.target!==sheet&&!r.target.closest?.('#ui-editor-panel,#ui-editor-toggle')&&[...r.addedNodes,...r.removedNodes].some(n=>n.nodeType===1)))schedule();}).observe(document.body,{childList:true,subtree:true});
 window.UIStyleRuntime={blank,key,excluded,groups,mapList,mapRole,setPopup(name,route){popup=(name+'-'+(route||'default')).replace(/[^\w.-]/g,'_');schedule();},apply(config){preview=true;current=structuredClone(config);refresh();for(const mp of mapList())mp.invalidateSize(false);},close(){preview=false;current=structuredClone(published);refresh();for(const mp of mapList())mp.invalidateSize(false);},published(config){published=structuredClone(config);if(!preview){current=structuredClone(config);refresh();}},refresh};
 fetch('/api/ui/published',{cache:'no-store'}).then(r=>r.ok?r.json():blank()).then(c=>{published=c;if(!preview){current=c;refresh();}}).catch(()=>{});
 // The protected script is requested only after the existing admin-session endpoint confirms access.
 fetch('/api/admin/session',{cache:'no-store'}).then(r=>r.json()).then(s=>{if(s.authenticated){const script=document.createElement('script');script.src='/api/admin/ui/editor.js';document.body.appendChild(script);}}).catch(()=>{});
 refresh();
})();
