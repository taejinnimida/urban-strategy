/* Rendering-only regression for lower 도시계획정보도. */
'use strict';
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('development-ui.js','utf8');
const start=source.indexOf('const DEV_PLANNING_FILL_OPACITY=');
const end=source.indexOf('function devPlanningRefresh(){',start);
assert(start>0&&end>start,'map block must exist');
const ctx={};
vm.createContext(ctx);
vm.runInContext(source.slice(start,end)+
  '\nthis.symbol=devPlanningSymbol;this.opacity=DEV_PLANNING_FILL_OPACITY;',ctx);
const feature=name=>({properties:{uname:name}});
assert.equal(ctx.opacity,0.75,'25% transparency = 75% opacity');
const cases=[
 ['제1종일반주거지역','#ffe9a0'],['제2종일반주거지역','#f6cf6d'],
 ['제3종일반주거지역','#efa54f'],['준주거지역','#f2be55'],
 ['일반상업지역','#c64978'],['준공업지역','#ba9bc9'],
 ['자연녹지지역','#aedb86'],['생산관리지역','#d2d9b5']];
for(const [name,color] of cases){
 const s=ctx.symbol('LT_C_UQ111',feature(name));
 assert.equal(s.fillColor,color,name);
 assert.equal(s.fillOpacity,0.75,name);
}
for(let n=1;n<=9;n++){
 const s=ctx.symbol('LT_C_UPISUQ15'+n,feature('시설'));
 assert.equal(s.fillOpacity,0.75);
}
assert.equal(ctx.symbol('LP_PA_CBND_BUBUN',feature('448-1')).fillOpacity,0,'parcel no fill');
assert.match(source,/도시·군관리계획도 색상·무늬 관례 참고\(면 채색 투명도 25%\)/);
assert.match(source,/devPlanningApplyPattern\(layer,symbol\)/);
console.log('PASS: city management lower map only; category colors, patterns, 25% transparency');
