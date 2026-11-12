// XDWorld / S-MAP bridge for Urban Landscape Review.
// This adapter only wraps capabilities confirmed in XDWorld public documentation.
// Runtime layer names / app credentials must be supplied by the S-MAP/OpenLab environment.

(function(){
  class LandscapeXDWorldAdapter{
    constructor(moduleRef){
      this.Module=moduleRef||window.Module||null;
    }
    isReady(){
      return !!this.Module;
    }
    boundaryRingsFromGeoJSON(geojson){
      const rings=[];
      const addGeom=(g)=>{
        if(!g)return;
        if(g.type==='Polygon' && Array.isArray(g.coordinates?.[0])) rings.push(g.coordinates[0].map(p=>[Number(p[0]),Number(p[1])]));
        if(g.type==='MultiPolygon') (g.coordinates||[]).forEach(poly=>{if(Array.isArray(poly?.[0]))rings.push(poly[0].map(p=>[Number(p[0]),Number(p[1])]));});
      };
      if(geojson?.type==='FeatureCollection') (geojson.features||[]).forEach(f=>addGeom(f.geometry));
      else if(geojson?.type==='Feature') addGeom(geojson.geometry);
      else addGeom(geojson);
      return rings.filter(r=>r.length>=3);
    }
    createBoundaryExcludeConfig(geojson){
      const rings=this.boundaryRingsFromGeoJSON(geojson);
      if(!rings.length) throw new Error('VALID_BOUNDARY_REQUIRED');
      return {mode:'exclude',rings:rings[0]};
    }
    createServerLayerWithBoundary(options,geojson){
      if(!this.Module?.getTileLayerList) throw new Error('XDWORLD_RUNTIME_REQUIRED');
      const config={...options,boundaryLimit:this.createBoundaryExcludeConfig(geojson)};
      return this.Module.getTileLayerList().createXDServerLayer(config);
    }
    getCamera(){
      if(!this.Module?.getViewCamera) throw new Error('XDWORLD_RUNTIME_REQUIRED');
      return this.Module.getViewCamera();
    }
    setPersonHeight(height=1.7){
      const camera=this.getCamera();
      if(typeof camera.setPersonHeight==='function') camera.setPersonHeight(Number(height));
      return camera;
    }
  }

  window.LandscapeXDWorldAdapter=LandscapeXDWorldAdapter;
})();
