const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
function setup(region='SEOUL'){
 const els=Object.fromEntries(['siteReviewBtn','devNational','devStatus'].map(k=>[k,{textContent:'',disabled:true}]));const opened=[];
 const geometry={type:'Polygon',coordinates:[[[126.97,37.56],[126.971,37.56],[126.971,37.561],[126.97,37.56]]]};
 const ctx={console,URLSearchParams,JSON,setTimeout,clearTimeout,Blob,URL,document:{documentElement:{dataset:{reviewMode:'select'}},getElementById:id=>els[id]},window:{open:url=>{opened.push(url);return {}; }},location:{search:''},activeGeometry:geometry,boundaryReferenceGeometry:null,selectedParcelPnus:new Set(),runSiteReview:()=>{throw Error('Seoul must not run on selection');},confirmCurrentBoundary:async()=>{},renderBoundaryParcelSelectionSummary:()=>{},measureAndSync:()=>{},setBoundaryReferenceMetrics:()=>{},runSchemeChecksWhenCoreReady:()=>{},updateCandidateSchemes:()=>{},refreshCompactMiniMaps:()=>{},r101Schedule:()=>{},loadBuiltInStationDataset:()=>{},loadBuiltInCenterDataset:()=>{},parseBoundaryAddressEntries:()=>({rows:[],invalid:[]}),resolveSeoulParcelAddress:()=>{},fetchParcelAtMapPoint:()=>{},fetch:async()=>({ok:true,json:async()=>({id:'test',geometry,region,area_m2:1000})})};vm.createContext(ctx);vm.runInContext(fs.readFileSync(__dirname+'/development-ui.js','utf8'),ctx);return {ctx,els,opened};
}
(async()=>{
 const {ctx,els,opened}=setup();await ctx.confirmCurrentBoundary();assert.equal(els.siteReviewBtn.disabled,false);assert.equal(els.devNational.disabled,false);ctx.devOpen('seoul');ctx.devOpen('development');assert.equal(opened.length,2);assert.match(opened[0],/review=seoul&site=test/);assert.match(opened[1],/review=development&site=test/);
 ctx.activeGeometry.coordinates[0][0][0]+=.001;ctx.devRefresh();assert.equal(els.siteReviewBtn.disabled,true);assert.equal(els.devNational.disabled,true);ctx.devOpen('development');assert.equal(opened.length,2);
 for(const region of ['UNKNOWN','OUTSIDE_OR_MIXED']){const c=setup(region);await c.ctx.confirmCurrentBoundary();assert.equal(c.els.siteReviewBtn.disabled,true);assert.equal(c.els.devNational.disabled,false);c.ctx.devOpen('seoul');assert.equal(c.opened.length,0);}
 const c=setup();c.ctx.fetch=async()=>({ok:false,json:async()=>({detail:'API error'})});await c.ctx.confirmCurrentBoundary();assert.equal(c.els.devNational.disabled,true);
 const cancel=setup();cancel.ctx.selectedParcelPnus.add('1');cancel.ctx.applySelectedParcelsAsBoundary=async()=>false;await cancel.ctx.confirmCurrentBoundary();assert.equal(cancel.els.devNational.disabled,true);
 console.log('PASS: confirm; two windows; edited boundary lock; outside/unknown Seoul lock; error; cancelled parcel confirmation');
})().catch(e=>{console.error(e);process.exit(1)});
