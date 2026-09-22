const fs=require('node:fs'), vm=require('node:vm'), assert=require('node:assert/strict');
const html=fs.readFileSync('app.html','utf8');
function extract(name){
 const start=html.indexOf(`async function ${name}(`);
 assert(start>=0,name);
 const rest=html.slice(start);const end=rest.slice(1).search(/\n(?:async )?function |\n(?:const|let) /);
 return end<0?rest:rest.slice(0,end+1);
}
async function testOrder(failFirst=false){
 const log=[];let release;
 const gate=new Promise(r=>release=r);
 const context={activeGeometry:{},currentAnalysisRunId:1,setSiteReviewStatus(){},renderIndependentSpatialStatusMaps(){},
  renewalAnalysis:{overlaps:[]},developmentAnalysis:{loaded:true,overlaps:[]},safeDowntownExclusionAnalysis:{known:true},
  analyzeRenewalZones:async()=>{log.push('renewal');await gate;if(failFirst)throw Error('fixture failure');},
  analyzeDevelopmentZones:async options=>{assert.equal(options.localOnly,true);log.push('development');},
  analyzeRegulatoryConstraints:async options=>{assert.equal(options.localOnly,true);log.push('regulatory');return {loaded:true,items:[]};},
  analyzeHillZones:async()=>{log.push('terrain');return {};},
  analyzeSafeDowntownExclusion:async()=>{log.push('downtown');},
  analyzeSchoolAbsoluteProtection:async()=>{log.push('school');return {};},
  analyzeSharedConservation:async options=>{assert.equal(options.localOnly,true);log.push('conservation');return {};},
  analyzeParcels:async()=>{log.push('external');throw Error('STOP_EXTERNAL');},
  safeAnalysisStep:async(label,fn)=>{try{return {label,status:'fulfilled',value:await fn()};}catch(e){if(e.message==='STOP_EXTERNAL')throw e;return {label,status:'rejected'};}},
 };
 vm.createContext(context);vm.runInContext(extract('runLocalAutoAnalyses')+'\n'+extract('runAllAutoAnalyses'),context);
 const running=context.runAllAutoAnalyses().catch(e=>assert.equal(e.message,'STOP_EXTERNAL'));
 await new Promise(r=>setImmediate(r));assert.deepEqual(log,['renewal']);
 release();await running;
 assert.deepEqual(log,['renewal','development','regulatory','terrain','downtown','school','conservation','external']);
}
async function testRoadGate(){
 let facilityRequests=0;
 const context={activeGeometry:{},performance:{now:()=>0},console:{info(){}},
 turf:{feature:g=>({geometry:g}),area:()=>4500,buffer:f=>f},
 schemeStreetBlockAreaPrefilter:()=>({activation:false}),markSchemeStreetBlockAreaSkipped(){},
 SCHEME_STREET_BLOCK_CONFIG:{activation:{}},schemeStreetBlockAnalysis:{},
 setSchemeStreetBlockProgress(){},streetBlockRuntimePreflight:async()=>({known:true,configured:true}),
 fetchIndependentRoadFacts:async()=>({manage_features:[{geometry:{},properties:{}}]}),
 schemeRoadWidthM:()=>null,newSchemeStreetBlockState:()=>({}),renderSchemeStreetBlockSpatialStatus(){},
 fetchSchemePlanningRoadBase:async()=>{facilityRequests++;throw Error('must not run');},
 };
 vm.createContext(context);vm.runInContext(extract('analyzeSchemeStreetBlocks'),context);
 const result=await context.analyzeSchemeStreetBlocks();
 assert.equal(result.data_status,'NO_DATA');assert.equal(facilityRequests,0);
 assert.equal(context.schemeStreetBlockAnalysis.activation.status,'review');
}
(async()=>{await testOrder();await testOrder(true);await testRoadGate();console.log('PASS: local-first order, local failure isolation, missing-width REVIEW gate (3 tests)');})().catch(e=>{console.error(e);process.exit(1);});
