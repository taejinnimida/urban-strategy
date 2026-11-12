// Urban Landscape Review - VWorld WebGL 3D API 3.0 adapter.
// Uses VWorld's public WebGL 3D API and Cesium objects exposed by the runtime.
// Confirmed patterns follow V-world/V-world_API_sample: vw.Map, ws3d.viewer,
// Cesium3DTileFeature.show, and viewer.entities.add(GLB).

(function(){
  class LandscapeVWorld3DAdapter {
    constructor(config={}){
      this.apiKey=String(config.apiKey||'').trim();
      this.domain=String(config.domain||location.origin||'').replace(/\/$/,'');
      this.map=null;
      this.viewer=null;
      this.containerId=null;
      this.planEntities=[];
      this.hiddenFeatures=new Set();
      this.eraseHandler=null;
      this.ready=false;
      this._loadPromise=null;
    }

    async loadApi(){
      if(window.vw && window.Cesium) return true;
      if(this._loadPromise) return this._loadPromise;
      if(!this.apiKey) throw new Error('VWORLD_API_KEY_REQUIRED');
      this._loadPromise=new Promise((resolve,reject)=>{
        const existing=document.querySelector('script[data-vworld-webgl3="1"]');
        if(existing){
          const poll=()=>window.vw&&window.Cesium?resolve(true):setTimeout(poll,100);
          poll();return;
        }
        const script=document.createElement('script');
        script.dataset.vworldWebgl3='1';
        script.async=true;
        script.src='https://map.vworld.kr/js/webglMapInit.js.do?version=3.0&apiKey='+encodeURIComponent(this.apiKey);
        script.onload=()=>{
          const started=Date.now();
          const poll=()=>{
            if(window.vw&&window.Cesium)return resolve(true);
            if(Date.now()-started>12000)return reject(new Error('VWORLD_WEBGL_INIT_TIMEOUT'));
            setTimeout(poll,100);
          };
          poll();
        };
        script.onerror=()=>reject(new Error('VWORLD_WEBGL_SCRIPT_LOAD_FAILED'));
        document.head.appendChild(script);
      });
      return this._loadPromise;
    }

    async init(containerId,center){
      await this.loadApi();
      if(this.ready && this.viewer) return this.viewer;
      this.containerId=containerId;
      const host=document.getElementById(containerId);
      if(!host) throw new Error('VWORLD_CONTAINER_NOT_FOUND');
      host.innerHTML='';
      const lon=Number(center?.[1]??126.9780),lat=Number(center?.[0]??37.5665);
      return new Promise((resolve,reject)=>{
        let done=false;
        const finish=(err)=>{
          if(done)return;done=true;
          if(err)return reject(err);
          try{
            this.viewer=window.ws3d?.viewer;
            if(!this.viewer)throw new Error('VWORLD_VIEWER_NOT_READY');
            this.ready=true;
            resolve(this.viewer);
          }catch(e){reject(e);}
        };
        const prev=window.vw.ws3dInitCallBack;
        window.vw.ws3dInitCallBack=()=>{
          try{if(typeof prev==='function')prev();}catch(_){}
          finish();
        };
        try{
          this.map=new window.vw.Map();
          this.map.setOption({
            mapId:containerId,
            initPosition:new window.vw.CameraPosition(
              new window.vw.CoordZ(lon,lat,900),
              new window.vw.Direction(0,-70,0)
            ),
            logo:true,
            navigation:true
          });
          this.map.start();
          setTimeout(()=>finish(),10000);
        }catch(e){finish(e);}
      });
    }

    _outerRings(geojson){
      const rings=[];
      const add=g=>{
        if(!g)return;
        if(g.type==='Polygon'&&Array.isArray(g.coordinates?.[0]))rings.push(g.coordinates[0]);
        if(g.type==='MultiPolygon')(g.coordinates||[]).forEach(p=>{if(Array.isArray(p?.[0]))rings.push(p[0]);});
      };
      if(geojson?.type==='FeatureCollection')(geojson.features||[]).forEach(f=>add(f.geometry));
      else if(geojson?.type==='Feature')add(geojson.geometry);
      else add(geojson);
      return rings.filter(r=>r.length>=3);
    }

    _bearing(lat1,lon1,lat2,lon2){
      const r=Math.PI/180,a=lat1*r,b=lat2*r,dl=(lon2-lon1)*r;
      const y=Math.sin(dl)*Math.cos(b);
      const x=Math.cos(a)*Math.sin(b)-Math.sin(a)*Math.cos(b)*Math.cos(dl);
      return (Math.atan2(y,x)*180/Math.PI+360)%360;
    }

    _distance(lat1,lon1,lat2,lon2){
      const R=6371000,r=Math.PI/180;
      const p1=lat1*r,p2=lat2*r,dp=(lat2-lat1)*r,dl=(lon2-lon1)*r;
      const a=Math.sin(dp/2)**2+Math.cos(p1)*Math.cos(p2)*Math.sin(dl/2)**2;
      return 2*R*Math.asin(Math.sqrt(a));
    }

    setViewpoint(viewpoint,target,cameraHeight=1.7,targetHeight=15,fov=60){
      if(!this.viewer)throw new Error('VWORLD_VIEWER_NOT_READY');
      const lon=Number(viewpoint.lng),lat=Number(viewpoint.lat);
      const tlon=Number(target.lng),tlat=Number(target.lat);
      const distance=Math.max(1,this._distance(lat,lon,tlat,tlon));
      const bearing=this._bearing(lat,lon,tlat,tlon);
      const pitch=Math.atan2(Number(targetHeight)-Number(cameraHeight),distance);
      this.viewer.camera.frustum.fov=Cesium.Math.toRadians(Number(fov)||60);
      this.viewer.camera.setView({
        destination:Cesium.Cartesian3.fromDegrees(lon,lat,Number(cameraHeight)||1.7),
        orientation:{
          heading:Cesium.Math.toRadians(bearing),
          pitch,
          roll:0
        }
      });
      return {distance,bearing,pitchDeg:Cesium.Math.toDegrees(pitch)};
    }

    clearPlan(){
      if(!this.viewer)return;
      this.planEntities.forEach(e=>{try{this.viewer.entities.remove(e);}catch(_){}});
      this.planEntities=[];
    }

    addPlanMasses(buildings){
      if(!this.viewer)throw new Error('VWORLD_VIEWER_NOT_READY');
      this.clearPlan();
      for(const [index,b] of (buildings||[]).entries()){
        const ring=b?.coords?.[0]||[];
        if(ring.length<3)continue;
        const flat=[];
        ring.forEach(p=>{flat.push(Number(p[0]),Number(p[1]));});
        const centroid=ring.reduce((a,p)=>[a[0]+Number(p[0]),a[1]+Number(p[1])],[0,0]).map(v=>v/ring.length);
        let ground=0;
        try{
          const carto=Cesium.Cartographic.fromDegrees(centroid[0],centroid[1]);
          const h=this.viewer.scene?.globe?.getHeight(carto);
          if(Number.isFinite(h))ground=h;
        }catch(_){}
        const height=Math.max(1,Number(b.height||50));
        const entity=this.viewer.entities.add({
          id:'landscape-plan-'+index,
          name:b.name||((index+1)+'동'),
          polygon:{
            hierarchy:Cesium.Cartesian3.fromDegreesArray(flat),
            height:ground,
            extrudedHeight:ground+height,
            material:Cesium.Color.fromCssColorString('#b47a3c').withAlpha(0.92),
            outline:true,
            outlineColor:Cesium.Color.fromCssColorString('#5d3a1c'),
            closeTop:true,
            closeBottom:true
          },
          properties:{landscapePlan:true,buildingIndex:index,height}
        });
        this.planEntities.push(entity);
      }
      this.viewer.scene.requestRender?.();
      return this.planEntities.length;
    }

    addGlbModel(url,options={}){
      if(!this.viewer)throw new Error('VWORLD_VIEWER_NOT_READY');
      const lon=Number(options.lon),lat=Number(options.lat),height=Number(options.height||0);
      const position=Cesium.Cartesian3.fromDegrees(lon,lat,height);
      const hpr=new Cesium.HeadingPitchRoll(Cesium.Math.toRadians(Number(options.heading||0)),0,0);
      const entity=this.viewer.entities.add({
        name:options.name||'계획 3D 모델',
        position,
        orientation:Cesium.Transforms.headingPitchRollQuaternion(position,hpr),
        model:{uri:url,scale:Number(options.scale||1),minimumPixelSize:16,maximumScale:20000}
      });
      this.planEntities.push(entity);
      return entity;
    }

    setPlanVisible(visible){
      this.planEntities.forEach(e=>{e.show=!!visible;});
      this.viewer?.scene?.requestRender?.();
    }

    setAllBuildingsVisible(visible){
      try{
        const layer=this.map?.getLayerElement?.('facility_build');
        if(layer){visible?layer.show():layer.hide();return true;}
      }catch(_){}
      return false;
    }

    enableManualBuildingErase(onHidden){
      if(!this.viewer)throw new Error('VWORLD_VIEWER_NOT_READY');
      this.disableManualBuildingErase();
      this.eraseHandler=new Cesium.ScreenSpaceEventHandler(this.viewer.canvas);
      this.eraseHandler.setInputAction(click=>{
        const picked=this.viewer.scene.pick(click.position);
        if(picked instanceof Cesium.Cesium3DTileFeature){
          picked.show=false;
          this.hiddenFeatures.add(picked);
          this.viewer.scene.requestRender?.();
          if(typeof onHidden==='function')onHidden(this.hiddenFeatures.size,picked);
        }
      },Cesium.ScreenSpaceEventType.LEFT_CLICK);
      return true;
    }

    disableManualBuildingErase(){
      if(this.eraseHandler){
        try{this.eraseHandler.destroy();}catch(_){}
        this.eraseHandler=null;
      }
    }

    restoreHiddenBuildings(){
      this.hiddenFeatures.forEach(f=>{try{f.show=true;}catch(_){}});
      const n=this.hiddenFeatures.size;
      this.hiddenFeatures.clear();
      this.viewer?.scene?.requestRender?.();
      return n;
    }

    _pointInScreenPolygon(x,y,ring){
      let inside=false;
      for(let i=0,j=ring.length-1;i<ring.length;j=i++){
        const xi=ring[i].x,yi=ring[i].y,xj=ring[j].x,yj=ring[j].y;
        const hit=((yi>y)!==(yj>y))&&(x<(xj-xi)*(y-yi)/(yj-yi+1e-9)+xi);
        if(hit)inside=!inside;
      }
      return inside;
    }

    async autoHideBuildingsInBoundary(geojson,options={}){
      if(!this.viewer)throw new Error('VWORLD_VIEWER_NOT_READY');
      const rings=this._outerRings(geojson);
      if(!rings.length)throw new Error('VALID_BOUNDARY_REQUIRED');
      const ring=rings.sort((a,b)=>b.length-a.length)[0];
      const xs=ring.map(p=>Number(p[0])),ys=ring.map(p=>Number(p[1]));
      const pad=Number(options.padDegrees||0.00035);
      const rect=Cesium.Rectangle.fromDegrees(Math.min(...xs)-pad,Math.min(...ys)-pad,Math.max(...xs)+pad,Math.max(...ys)+pad);
      const camera=this.viewer.camera;
      const saved={
        position:Cesium.Cartesian3.clone(camera.positionWC),
        direction:Cesium.Cartesian3.clone(camera.directionWC),
        up:Cesium.Cartesian3.clone(camera.upWC)
      };
      camera.setView({destination:rect});
      await new Promise(r=>setTimeout(r,Number(options.waitMs||1600)));
      const projected=ring.map(p=>{
        const pos=Cesium.Cartesian3.fromDegrees(Number(p[0]),Number(p[1]),0);
        const fn=Cesium.SceneTransforms.worldToWindowCoordinates||Cesium.SceneTransforms.wgs84ToWindowCoordinates;
        return fn?fn(this.viewer.scene,pos):null;
      }).filter(Boolean);
      if(projected.length<3){
        camera.setView({destination:saved.position,orientation:{direction:saved.direction,up:saved.up}});
        throw new Error('BOUNDARY_SCREEN_PROJECT_FAILED');
      }
      const minX=Math.max(0,Math.floor(Math.min(...projected.map(p=>p.x))));
      const maxX=Math.min(this.viewer.canvas.clientWidth,Math.ceil(Math.max(...projected.map(p=>p.x))));
      const minY=Math.max(0,Math.floor(Math.min(...projected.map(p=>p.y))));
      const maxY=Math.min(this.viewer.canvas.clientHeight,Math.ceil(Math.max(...projected.map(p=>p.y))));
      const step=Math.max(4,Number(options.stepPx||7));
      let hits=0;
      for(let y=minY;y<=maxY;y+=step){
        for(let x=minX;x<=maxX;x+=step){
          if(!this._pointInScreenPolygon(x,y,projected))continue;
          const picked=this.viewer.scene.pick(new Cesium.Cartesian2(x,y));
          if(picked instanceof Cesium.Cesium3DTileFeature && !this.hiddenFeatures.has(picked)){
            picked.show=false;this.hiddenFeatures.add(picked);hits++;
          }
        }
      }
      camera.setView({destination:saved.position,orientation:{direction:saved.direction,up:saved.up}});
      this.viewer.scene.requestRender?.();
      return {hidden:hits,totalHidden:this.hiddenFeatures.size,mode:'screen-scan-beta'};
    }
  }

  window.LandscapeVWorld3DAdapter=LandscapeVWorld3DAdapter;
})();
