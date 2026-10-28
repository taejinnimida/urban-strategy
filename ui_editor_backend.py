"""UI-only settings, typed allowlist, durable versioned storage. No analysis imports."""
import os, re, json, hmac, hashlib, sqlite3
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlsplit
from fastapi import Depends, HTTPException, Request
from fastapi.responses import Response

EMPTY = {"schema": 1, "elements": {}, "maps": {}}
LENGTHS = {'legend-symbol-width','legend-symbol-height','font-size','width','height','min-width','max-width','min-height','max-height','padding-top','padding-right','padding-bottom','padding-left','margin-top','margin-right','margin-bottom','margin-left','gap','row-gap','column-gap','border-width','border-radius','letter-spacing'}
ENUMS = {'font-weight':['normal','bold','100','200','300','400','500','600','700','800','900'], 'text-align':['left','center','right','justify'], 'display':['block','inline-block','flex','grid'], 'flex-direction':['row','column','row-reverse','column-reverse'], 'flex-wrap':['nowrap','wrap'], 'align-items':['stretch','flex-start','center','flex-end','baseline'], 'justify-content':['flex-start','center','flex-end','space-between','space-around','space-evenly'], 'border-style':['none','solid','dashed','dotted','double']}
COLORS = {'color','background-color','border-color'}
KEY = re.compile(r'^(element|common|popup):[A-Za-z0-9_.:-]{1,180}$')
COLOR = re.compile(r'^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$')

def validate(config):
    if not isinstance(config,dict) or set(config)!={'schema','elements','maps'} or config['schema']!=1:
        raise HTTPException(422,'UI 설정 형식 오류')
    if not isinstance(config['elements'],dict) or len(config['elements'])>500 or not isinstance(config['maps'],dict) or len(config['maps'])>100:
        raise HTTPException(422,'UI 설정 개수 초과')
    if len(json.dumps(config))>150000: raise HTTPException(413,'설정 크기 초과')
    for key,styles in config['elements'].items():
        if not KEY.fullmatch(key) or not isinstance(styles,dict) or len(styles)>60: raise HTTPException(422,'요소 식별자 오류')
        for prop,v in styles.items():
            if not isinstance(v,str) or len(v)>80: raise HTTPException(422,'스타일 값 오류')
            ok=False
            if prop in LENGTHS:
                match=re.fullmatch(r'(\d+(?:\.\d{1,3})?)(px|rem|em|%|vh|vw)',v)
                ok=bool(match and 0<=float(match[1])<=({'px':2400,'%':100,'vh':100,'vw':100,'rem':150,'em':150}[match[2]])) or v=='auto' and prop in {'width','height','margin-left','margin-right'} or v=='none' and prop in {'max-width','max-height'}
            elif prop in ENUMS: ok=v in ENUMS[prop]
            elif prop in COLORS: ok=bool(COLOR.fullmatch(v)) or v=='transparent'
            elif prop=='line-height':
                try: ok=0.5<=float(v)<=4
                except ValueError: pass
            elif prop=='grid-template-columns': ok=bool(re.fullmatch(r'repeat\([1-6], minmax\(0, 1fr\)\)',v))
            if not ok: raise HTTPException(422,'허용되지 않은 스타일: '+prop)
    for key,styles in config['maps'].items():
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,100}:(site|theme|background)',key) or not isinstance(styles,dict): raise HTTPException(422,'지도 식별자 오류')
        for prop,v in styles.items():
            if prop in {'color','fillColor'}: ok=isinstance(v,str) and bool(COLOR.fullmatch(v))
            else: ok=type(v) in (int,float) and prop in {'weight','opacity','fillOpacity','labelSize'} and 0<=v<=({'weight':12,'opacity':1,'fillOpacity':1,'labelSize':32}.get(prop,0))
            if not ok: raise HTTPException(422,'지도 스타일 오류')
    return config

class Store:
    def __init__(self):
        self.url=os.getenv('DATABASE_URL','').strip()
        self.path=os.getenv('UI_SETTINGS_DB_PATH','/var/data/urban_ui.sqlite3').strip()
        root=os.getenv('UI_SETTINGS_DISK_ROOT','/var/data')
        self.durable=bool(self.url) or (os.path.ismount(root) and Path(self.path).resolve().is_relative_to(Path(root).resolve()))
        self.local=not os.getenv('RENDER') and os.getenv('UI_SETTINGS_ALLOW_LOCAL')=='1'
    def info(self):
        return {'mode':'postgresql' if self.url else 'persistent_disk' if self.durable else 'local_test' if self.local else 'unconfigured','writable':self.durable or self.local,'durable':self.durable}
    @contextmanager
    def connect(self):
        if not self.info()['writable']: raise HTTPException(503,'영구 저장소 필요: DATABASE_URL 또는 Render Disk를 설정하세요.')
        if self.url:
            import psycopg
            c=psycopg.connect(self.url,connect_timeout=8)
        else:
            Path(self.path).parent.mkdir(parents=True,exist_ok=True)
            c=sqlite3.connect(self.path,timeout=15);c.execute('PRAGMA journal_mode=WAL');c.execute('BEGIN IMMEDIATE')
        try:
            c.execute('CREATE TABLE IF NOT EXISTS urban_ui_state (id INTEGER PRIMARY KEY, revision INTEGER NOT NULL, draft TEXT NOT NULL, published TEXT NOT NULL)')
            c.execute('CREATE TABLE IF NOT EXISTS urban_ui_history (revision INTEGER PRIMARY KEY, action TEXT NOT NULL, created TEXT NOT NULL, config TEXT NOT NULL)')
            empty=json.dumps(EMPTY)
            self.sql(c,'INSERT INTO urban_ui_state VALUES (1,0,?,?) ON CONFLICT(id) DO NOTHING',(empty,empty))
            yield c;c.commit()
        except Exception: c.rollback();raise
        finally:c.close()
    def sql(self,c,s,args=()):return c.execute(s.replace('?', '%s') if self.url else s,args)
    def state(self,c,lock=False):
        return self.sql(c,'SELECT revision,draft,published FROM urban_ui_state WHERE id=1'+(' FOR UPDATE' if lock and self.url else '')).fetchone()
    def read(self):
        with self.connect() as c:
            rev,draft,published=self.state(c)
            history=self.sql(c,'SELECT revision,action,created FROM urban_ui_history ORDER BY revision DESC LIMIT 50').fetchall()
            return {'revision':rev,'draft':json.loads(draft),'published':json.loads(published),'history':[dict(zip(['revision','action','created'],x)) for x in history],'storage':self.info()}
    def mutate(self,action,body):
        if set(body)-{'expected_revision','config','source_revision'}:raise HTTPException(422,'지원하지 않는 설정 필드')
        if type(body.get('expected_revision')) is not int:raise HTTPException(422,'설정 버전 필요')
        with self.connect() as c:
            rev,draft,published=self.state(c,True)
            if body['expected_revision']!=rev:raise HTTPException(409,'다른 창에서 설정이 변경됐습니다. 최신 설정을 다시 불러오세요.')
            if action=='save':draft=json.dumps(validate(body.get('config')),ensure_ascii=False)
            elif action=='defaults':draft=json.dumps(EMPTY)
            elif action=='restore':
                if type(body.get('source_revision')) is not int:raise HTTPException(422,'복구할 버전 필요')
                row=self.sql(c,'SELECT config FROM urban_ui_history WHERE revision=?',(body['source_revision'],)).fetchone()
                if not row:raise HTTPException(404,'복구할 버전 없음')
                draft=json.dumps(validate(json.loads(row[0])),ensure_ascii=False)
            elif action=='publish':published=draft
            else:raise HTTPException(404)
            rev+=1
            self.sql(c,'UPDATE urban_ui_state SET revision=?,draft=?,published=? WHERE id=1',(rev,draft,published))
            self.sql(c,'INSERT INTO urban_ui_history VALUES (?,?,?,?)',(rev,action,datetime.now(timezone.utc).isoformat(),draft))
        return self.read()

def install(app,auth,base_dir):
    root=Path(base_dir)/'ui_editor'
    def asset(name):return Response((root/name).read_text(),media_type='application/javascript',headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
    def csrf():return hmac.new(os.getenv('ADMIN_PASSWORD','').encode(),b'urban-ui-editor-csrf-v1',hashlib.sha256).hexdigest()
    def guard(request):
        token=request.headers.get('X-UI-CSRF','')
        if not token or not hmac.compare_digest(token,csrf()):raise HTTPException(403,'편집 세션을 다시 여세요.')
        if request.headers.get('sec-fetch-site')=='cross-site':raise HTTPException(403,'외부 요청 거부')
        origin=request.headers.get('origin')
        if origin and urlsplit(origin).netloc!=request.url.netloc:raise HTTPException(403,'요청 출처 오류')
    @app.get('/assets/ui-runtime.js')
    def runtime():return asset('runtime.js')
    @app.get('/api/ui/published')
    def public():
        try:config=Store().read()['published']
        except Exception:config=EMPTY
        return Response(json.dumps(config),media_type='application/json',headers={'Cache-Control':'no-store'})
    @app.get('/api/admin/ui/editor.js',dependencies=[Depends(auth)])
    def editor():return asset('editor.js')
    @app.get('/api/admin/ui/state',dependencies=[Depends(auth)])
    def state():
        store=Store()
        if not store.info()['writable']:return {**{'revision':0,'draft':EMPTY,'published':EMPTY,'history':[]},'storage':store.info(),'csrf':csrf()}
        try:return {**store.read(),'csrf':csrf()}
        except HTTPException:raise
        except Exception:raise HTTPException(503,'UI 저장소 연결 실패. 관리자 설정을 확인하세요.') from None
    @app.post('/api/admin/ui/{action}',dependencies=[Depends(auth)])
    async def mutate(action:str,request:Request):
        guard(request)
        raw=await request.body()
        if len(raw)>180000:raise HTTPException(413,'설정 크기 초과')
        try:body=json.loads(raw)
        except Exception:raise HTTPException(422,'JSON 형식 오류') from None
        if not isinstance(body,dict):raise HTTPException(422,'설정 형식 오류')
        try:return {**Store().mutate(action,body),'csrf':csrf()}
        except HTTPException:raise
        except Exception:raise HTTPException(503,'UI 저장 실패. 운영 설정은 변경하지 않았습니다.') from None
