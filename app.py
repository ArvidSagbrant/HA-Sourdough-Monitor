import json, logging, os, shutil, sqlite3, statistics, subprocess, threading, time
from collections import deque
from contextlib import contextmanager
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit, urlparse, parse_qs

import cv2
import numpy as np
import paho.mqtt.client as mqtt
import requests

LOG = logging.getLogger('sourdough')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

OPTIONS_PATH=Path('/data/options.json'); ROI_OVERRIDE_PATH=Path('/data/roi.json'); UI_PATH=Path('/app/ui.html')
MEDIA_ROOT=Path('/media/sourdough'); DB_PATH=Path('/data/sourdough_journal.db')
DEVICE_ID='sourdough_monitor'; BASE_TOPIC='sourdough_monitor'; DISCOVERY_PREFIX='homeassistant'; VERSION='0.2.0'

session_lock=threading.Lock(); cfg_lock=threading.RLock()
session_active=False; session_dir=None; session_started=None; baseline_height_px=None; frame_no=0; last_timelapse_build=0.0
edge_history=deque(maxlen=5); last_growth_values=deque(maxlen=10); peak_growth=-1.0; peak_frame_path=None; start_frame_path=None; last_frame_path=None
mqtt_client=None; cfg={}; active_bake_id=None

SCHEMA='''
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS bakes (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', started_at TEXT NOT NULL, finished_at TEXT,
 recipe_json TEXT NOT NULL DEFAULT '{}', process_json TEXT NOT NULL DEFAULT '{}', bake_json TEXT NOT NULL DEFAULT '{}',
 result_json TEXT NOT NULL DEFAULT '{}', media_json TEXT NOT NULL DEFAULT '{}', notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, bake_id TEXT NOT NULL, kind TEXT NOT NULL, ts TEXT NOT NULL, data_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS measurements (seq INTEGER PRIMARY KEY AUTOINCREMENT, bake_id TEXT NOT NULL, session TEXT, ts TEXT NOT NULL, growth REAL, height_px INTEGER, edge_y INTEGER, confidence REAL, status TEXT);
CREATE INDEX IF NOT EXISTS idx_events_bake_ts ON events(bake_id,ts);
CREATE INDEX IF NOT EXISTS idx_measurements_bake_ts ON measurements(bake_id,ts);
'''
JSON_COLS=('recipe','process','bake','result','media')

def now_iso(): return datetime.now().astimezone().isoformat(timespec='seconds')
@contextmanager
def db():
    c=sqlite3.connect(DB_PATH, timeout=10); c.row_factory=sqlite3.Row
    try: yield c; c.commit()
    finally: c.close()
def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as c: c.executescript(SCHEMA)
def decode_bake(r):
    d=dict(r)
    for k in JSON_COLS: d[k]=json.loads(d.pop(k+'_json') or '{}')
    return d
def next_bake_id():
    day=datetime.now().astimezone().date().isoformat()
    with db() as c: rows=c.execute('SELECT id FROM bakes WHERE id LIKE ?', (day+'-%',)).fetchall()
    nums=[]
    for r in rows:
        try: nums.append(int(r['id'].rsplit('-',1)[1]))
        except: pass
    return f'{day}-{max(nums,default=0)+1:02d}'
def add_event(bid,kind,data=None):
    with db() as c: c.execute('INSERT INTO events(bake_id,kind,ts,data_json) VALUES(?,?,?,?)',(bid,kind,now_iso(),json.dumps(data or {},ensure_ascii=False)))
def get_bake(bid):
    with db() as c: r=c.execute('SELECT * FROM bakes WHERE id=?',(bid,)).fetchone()
    if not r: raise KeyError(bid)
    return decode_bake(r)
def list_bakes(limit=50):
    with db() as c: rows=c.execute('SELECT * FROM bakes ORDER BY started_at DESC LIMIT ?', (limit,)).fetchall()
    return [decode_bake(r) for r in rows]
def create_bake(p):
    global active_bake_id
    ts=now_iso(); bid=p.get('id') or next_bake_id(); name=(p.get('name') or 'Nytt surdegsbröd').strip()
    vals=[json.dumps(p.get(k) or {},ensure_ascii=False) for k in JSON_COLS]
    with db() as c:
        c.execute('''INSERT INTO bakes(id,name,status,started_at,recipe_json,process_json,bake_json,result_json,media_json,notes,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',(bid,name,p.get('status','active'),p.get('started_at') or ts,*vals,p.get('notes',''),ts,ts))
    active_bake_id=bid; add_event(bid,'created',{'name':name}); publish_journal_state(); return get_bake(bid)
def update_bake(bid,p):
    cur=get_bake(bid); sets=[]; args=[]
    for k in ('name','status','started_at','finished_at','notes'):
        if k in p: sets.append(k+'=?'); args.append(p[k])
    for k in JSON_COLS:
        if k in p:
            merged=dict(cur.get(k) or {}); merged.update(p[k] or {}); sets.append(k+'_json=?'); args.append(json.dumps(merged,ensure_ascii=False))
    sets.append('updated_at=?'); args.append(now_iso()); args.append(bid)
    with db() as c: c.execute('UPDATE bakes SET '+', '.join(sets)+' WHERE id=?',args)
    add_event(bid,'updated',p); publish_journal_state(); return get_bake(bid)
def phase_bake(bid,phase,data=None):
    keys={'starter_used':'starter_used_at','bulk_start':'bulk_start','bulk_end':'bulk_end','proof_start':'proof_start','proof_end':'proof_end','baked':'baked_at'}
    if phase not in keys: raise ValueError('Okänd fas')
    patch={'process':{keys[phase]:now_iso(), **(data or {})}}
    if phase=='baked': patch.update(status='finished',finished_at=now_iso())
    out=update_bake(bid,patch); add_event(bid,phase,data or {}); return out
def set_active_bake(bid):
    global active_bake_id
    if bid is not None: get_bake(bid)
    active_bake_id=bid; publish_journal_state()
def add_measurement(bid,session,growth,height,edge,conf,status):
    if not bid: return
    with db() as c: c.execute('INSERT INTO measurements(bake_id,session,ts,growth,height_px,edge_y,confidence,status) VALUES(?,?,?,?,?,?,?,?)',(bid,session,now_iso(),growth,height,edge,conf,status))
def bake_detail(bid):
    out=get_bake(bid)
    with db() as c:
        out['events']=[dict(r) for r in c.execute('SELECT seq,kind,ts,data_json FROM events WHERE bake_id=? ORDER BY ts',(bid,)).fetchall()]
        for e in out['events']: e['data']=json.loads(e.pop('data_json') or '{}')
        m=c.execute('SELECT COUNT(*) n, MIN(growth) min_growth, MAX(growth) max_growth, AVG(growth) avg_growth FROM measurements WHERE bake_id=?',(bid,)).fetchone()
        out['measurement_summary']=dict(m)
    return out

def load_options(): return json.loads(OPTIONS_PATH.read_text())
def load_roi_override():
    try:
        d=json.loads(ROI_OVERRIDE_PATH.read_text()) if ROI_OVERRIDE_PATH.exists() else {}; allowed={'roi_x_pct','roi_y_pct','roi_width_pct','roi_height_pct'}; return {k:int(v) for k,v in d.items() if k in allowed}
    except: LOG.exception('Kunde inte läsa ROI override'); return {}
def validate_roi(v):
    x,y,w,h=[int(round(float(v[k]))) for k in ('roi_x_pct','roi_y_pct','roi_width_pct','roi_height_pct')]
    if not(0<=x<=95 and 0<=y<=95 and 5<=w<=100 and 5<=h<=100) or x+w>100 or y+h>100: raise ValueError('Ogiltig ROI')
    return dict(roi_x_pct=x,roi_y_pct=y,roi_width_pct=w,roi_height_pct=h)
def current_roi():
    with cfg_lock: return {k:int(cfg[k]) for k in ('roi_x_pct','roi_y_pct','roi_width_pct','roi_height_pct')}
def save_roi_override(v):
    clean=validate_roi(v); tmp=ROI_OVERRIDE_PATH.with_suffix('.tmp'); tmp.write_text(json.dumps(clean,indent=2)); tmp.replace(ROI_OVERRIDE_PATH)
    with cfg_lock: cfg.update(clean)
    return clean
def clear_roi_override():
    if ROI_OVERRIDE_PATH.exists(): ROI_OVERRIDE_PATH.unlink()
    base=load_options()
    with cfg_lock:
        for k in ('roi_x_pct','roi_y_pct','roi_width_pct','roi_height_pct'): cfg[k]=base[k]
    return current_roi()

def get_mqtt_service(): return str(cfg['mqtt_host']).strip(),int(cfg.get('mqtt_port',1883)),str(cfg.get('mqtt_username','') or ''),str(cfg.get('mqtt_password','') or ''),bool(cfg.get('mqtt_tls',False))
def publish(topic,payload,retain=False):
    if mqtt_client is None:return
    if not isinstance(payload,str): payload=json.dumps(payload,ensure_ascii=False)
    mqtt_client.publish(topic,payload,qos=0,retain=retain)
def publish_binary(topic,payload,retain=False):
    if mqtt_client: mqtt_client.publish(topic,payload,qos=0,retain=retain)
def device_block(): return {'identifiers':[DEVICE_ID],'name':'Sourdough Monitor','manufacturer':'Local Home Assistant Add-on','model':'OpenCV Sourdough Monitor','sw_version':VERSION}
def publish_discovery():
    dev=device_block(); av=f'{BASE_TOPIC}/availability'
    for oid,name,unit,icon,sc in [('growth','Surdeg tillväxt','%','mdi:chart-line','measurement'),('height','Surdeg höjd','px','mdi:arrow-expand-vertical','measurement'),('edge_y','Surdeg topp Y','px','mdi:axis-y-arrow','measurement'),('frames','Surdeg bilder',None,'mdi:image-multiple','total_increasing'),('elapsed','Surdeg tid','min','mdi:timer-outline','measurement')]:
        p={'name':name,'unique_id':f'{DEVICE_ID}_{oid}','state_topic':f'{BASE_TOPIC}/state/{oid}','availability_topic':av,'device':dev,'icon':icon}
        if unit:p['unit_of_measurement']=unit
        if sc:p['state_class']=sc
        publish(f'{DISCOVERY_PREFIX}/sensor/{DEVICE_ID}/{oid}/config',p,True)
    for oid,name,icon in [('status','Surdeg status','mdi:state-machine'),('session','Surdeg session','mdi:identifier'),('active_bake','Aktivt bak','mdi:bread-slice')]:
        publish(f'{DISCOVERY_PREFIX}/sensor/{DEVICE_ID}/{oid}/config',{'name':name,'unique_id':f'{DEVICE_ID}_{oid}','state_topic':f'{BASE_TOPIC}/state/{oid}','json_attributes_topic':f'{BASE_TOPIC}/attributes/{oid}' if oid=='active_bake' else None,'availability_topic':av,'device':dev,'icon':icon},True)
    publish(f'{DISCOVERY_PREFIX}/image/{DEVICE_ID}/preview/config',{'name':'Surdeg preview','unique_id':f'{DEVICE_ID}_preview','image_topic':f'{BASE_TOPIC}/image/preview','content_type':'image/jpeg','availability_topic':av,'device':dev},True)
    publish(f'{DISCOVERY_PREFIX}/sensor/{DEVICE_ID}/timelapse/config',{'name':'Surdeg timelapse','unique_id':f'{DEVICE_ID}_timelapse','state_topic':f'{BASE_TOPIC}/state/timelapse','json_attributes_topic':f'{BASE_TOPIC}/attributes/timelapse','availability_topic':av,'device':dev,'icon':'mdi:movie-open'},True)
    publish(f'{DISCOVERY_PREFIX}/binary_sensor/{DEVICE_ID}/active/config',{'name':'Surdeg övervakning','unique_id':f'{DEVICE_ID}_active','state_topic':f'{BASE_TOPIC}/state/active','payload_on':'ON','payload_off':'OFF','availability_topic':av,'device':dev,'icon':'mdi:camera-timer'},True)
    for oid,name,icon in [('start','Starta surdeg','mdi:play'),('stop','Stoppa och bygg timelapse','mdi:stop'),('build','Bygg timelapse nu','mdi:movie-open-plus')]: publish(f'{DISCOVERY_PREFIX}/button/{DEVICE_ID}/{oid}/config',{'name':name,'unique_id':f'{DEVICE_ID}_{oid}','command_topic':f'{BASE_TOPIC}/cmd/{oid}','payload_press':'PRESS','availability_topic':av,'device':dev,'icon':icon},True)
    publish(av,'online',True); publish_journal_state()
def publish_journal_state():
    if active_bake_id:
        try: b=get_bake(active_bake_id); publish(f'{BASE_TOPIC}/state/active_bake',b['name'],True); publish(f'{BASE_TOPIC}/attributes/active_bake',{'id':b['id'],'status':b['status'],'started_at':b['started_at']},True); return
        except KeyError: pass
    publish(f'{BASE_TOPIC}/state/active_bake','none',True); publish(f'{BASE_TOPIC}/attributes/active_bake',{},True)

def camera_url_with_credentials(url):
    u=str(cfg.get('camera_username','') or ''); p=str(cfg.get('camera_password','') or '')
    if not u:return url
    parts=urlsplit(url)
    if '@' in parts.netloc:return url
    auth=quote(u,safe='')+(':'+quote(p,safe='') if p else '')
    return urlunsplit((parts.scheme,f'{auth}@{parts.netloc}',parts.path,parts.query,parts.fragment))
def fetch_snapshot_frame():
    auth=(cfg['camera_username'],cfg.get('camera_password','')) if cfg.get('camera_username') else None; r=requests.get(cfg['camera_url'],auth=auth,timeout=15); r.raise_for_status(); img=cv2.imdecode(np.frombuffer(r.content,np.uint8),cv2.IMREAD_COLOR)
    if img is None: raise RuntimeError('Snapshot kunde inte avkodas'); return img
    return img
def fetch_rtsp_frame():
    cmd=['ffmpeg','-hide_banner','-loglevel','error','-rtsp_transport',str(cfg.get('rtsp_transport','tcp')),'-i',camera_url_with_credentials(cfg['camera_url']),'-map','0:v:0','-frames:v','1','-f','image2pipe','-vcodec','mjpeg','pipe:1']; p=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=20)
    if p.returncode or not p.stdout: raise RuntimeError('ffmpeg kunde inte läsa RTSP: '+p.stderr.decode(errors='replace'))
    img=cv2.imdecode(np.frombuffer(p.stdout,np.uint8),cv2.IMREAD_COLOR)
    if img is None: raise RuntimeError('RTSP-frame kunde inte avkodas')
    return img
def fetch_frame(): return fetch_snapshot_frame() if str(cfg.get('camera_source','snapshot')).lower()=='snapshot' else fetch_rtsp_frame()
def roi_from_image(img):
    h,w=img.shape[:2]; r=current_roi(); x=int(w*r['roi_x_pct']/100); y=int(h*r['roi_y_pct']/100); x2=min(w,x+int(w*r['roi_width_pct']/100)); y2=min(h,y+int(h*r['roi_height_pct']/100)); return x,y,x2,y2
def detect_top(img):
    x1,y1,x2,y2=roi_from_image(img); crop=img[y1:y2,x1:x2]; gray=cv2.GaussianBlur(cv2.cvtColor(crop,cv2.COLOR_BGR2GRAY),(7,7),0); score=np.mean(np.abs(cv2.Sobel(gray,cv2.CV_32F,0,1,ksize=3)),axis=1); margin=max(5,int(len(score)*.05)); usable=score[margin:len(score)-margin]
    ly=int(np.argmax(usable))+margin; conf=float(score[ly]/(np.mean(usable)+1e-6)); gy=y1+ly; edge_history.append(gy); sy=int(statistics.median(edge_history)); return sy,max(1,y2-sy),conf,(x1,y1,x2,y2)
def annotate(img,edge,roi,growth,conf,status):
    x1,y1,x2,y2=roi; out=img.copy(); cv2.rectangle(out,(x1,y1),(x2,y2),(255,255,255),2); cv2.line(out,(x1,edge),(x2,edge),(255,255,255),3); cv2.putText(out,f'{growth:.1f}%  conf {conf:.2f}  {status}',(x1,max(30,y1-10)),cv2.FONT_HERSHEY_SIMPLEX,.8,(255,255,255),2,cv2.LINE_AA); return out
def infer_status(g):
    last_growth_values.append(g)
    if g<105:return 'start'
    if len(last_growth_values)>=5:
        v=list(last_growth_values)
        if g>=130 and statistics.mean(v[-2:])<statistics.mean(v[-5:-2])-1:return 'falling'
        if g>=180:return 'doubled'
    if g>=150:return 'strong_rise'
    if g>=110:return 'rising'
    return 'start'
def media_uri(path): return 'media-source://media_source/local/'+path.relative_to(Path('/media')).as_posix()
def publish_timelapse_state(state,session=None,frames=None,duration_seconds=None,path=None):
    publish(f'{BASE_TOPIC}/state/timelapse',state,True); a={}
    if session:a['session']=session
    if frames is not None:a['frames']=int(frames)
    if duration_seconds is not None:a['duration_seconds']=round(float(duration_seconds),1)
    if path:a.update(media_content_id=media_uri(path),media_content_type='video/mp4',filename=path.name)
    publish(f'{BASE_TOPIC}/attributes/timelapse',a,True)
def build_timelapse():
    global last_timelapse_build
    with session_lock: current=session_dir
    if not current:return
    frames=sorted((current/'frames').glob('*.jpg'))
    if len(frames)<2:return
    out=current/'timelapse.mp4'; tmp=current/'timelapse.tmp.mp4'; fps=int(cfg['timelapse_fps']); subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-framerate',str(fps),'-i',str(current/'frames'/'%06d.jpg'),'-c:v','libx264','-preset','veryfast','-crf','22','-pix_fmt','yuv420p','-movflags','+faststart',str(tmp)],check=True); tmp.replace(out); latest=MEDIA_ROOT/'latest.mp4'; shutil.copy2(out,latest); publish_timelapse_state('ready',current.name,len(frames),len(frames)/max(1,fps),latest); last_timelapse_build=time.time()
    if active_bake_id: update_bake(active_bake_id,{'media':{'timelapse':media_uri(out),'session':current.name}})
def finalize_keyframes_and_cleanup():
    with session_lock: current=session_dir; ss=start_frame_path; ps=peak_frame_path; es=last_frame_path
    if not current:return
    media={}
    for src,name in [(ss,'start.jpg'),(ps,'peak.jpg'),(es,'end.jpg')]:
        if src and Path(src).exists(): shutil.copy2(src,current/name); media[name.split('.')[0]]=media_uri(current/name)
    if active_bake_id and media: update_bake(active_bake_id,{'media':media})
    if (current/'frames').exists(): shutil.rmtree(current/'frames',ignore_errors=True)
def prune_sessions():
    sessions=sorted([p for p in MEDIA_ROOT.iterdir() if p.is_dir() and p.name.startswith('session_')],key=lambda p:p.stat().st_mtime,reverse=True)
    for old in sessions[int(cfg['keep_sessions']):]: shutil.rmtree(old,ignore_errors=True)
def start_session():
    global session_active,session_dir,session_started,baseline_height_px,frame_no,last_timelapse_build,peak_growth,peak_frame_path,start_frame_path,last_frame_path
    with session_lock:
        if session_active:return
        session_dir=MEDIA_ROOT/('session_'+datetime.now().strftime('%Y%m%d-%H%M%S')); (session_dir/'frames').mkdir(parents=True,exist_ok=True); session_started=datetime.now(); baseline_height_px=None; frame_no=0; edge_history.clear(); last_growth_values.clear(); peak_growth=-1.; peak_frame_path=start_frame_path=last_frame_path=None; session_active=True; last_timelapse_build=time.time()
    if active_bake_id: update_bake(active_bake_id,{'media':{'session':session_dir.name},'process':{'starter_monitor_started_at':now_iso()}})
    publish(f'{BASE_TOPIC}/state/active','ON',True); publish(f'{BASE_TOPIC}/state/session',session_dir.name,True); publish(f'{BASE_TOPIC}/state/status','calibrating',True); publish_timelapse_state('recording',session_dir.name,0); prune_sessions()
def stop_session():
    global session_active
    with session_lock: was=session_active; session_active=False; name=session_dir.name if session_dir else None; frames=frame_no
    if was:
        publish_timelapse_state('building',name,frames)
        try: build_timelapse(); finalize_keyframes_and_cleanup(); publish(f'{BASE_TOPIC}/state/status','stopped',True)
        except Exception: LOG.exception('Timelapse-bygge misslyckades'); publish_timelapse_state('error',name,frames)
    publish(f'{BASE_TOPIC}/state/active','OFF',True)
def process_frame():
    global frame_no,baseline_height_px,peak_growth,peak_frame_path,start_frame_path,last_frame_path
    img=fetch_frame(); edge,height,conf,roi=detect_top(img)
    with session_lock:
        if baseline_height_px is None: baseline_height_px=height
        growth=100.*height/max(1,baseline_height_px); status=infer_status(growth); ann=annotate(img,edge,roi,growth,conf,status); fp=session_dir/'frames'/f'{frame_no:06d}.jpg'; cv2.imwrite(str(fp),ann,[cv2.IMWRITE_JPEG_QUALITY,88])
        if frame_no==0:start_frame_path=fp
        if growth>peak_growth: peak_growth=growth; peak_frame_path=fp
        last_frame_path=fp; frame_no+=1; cv2.imwrite(str(MEDIA_ROOT/'latest.jpg'),ann,[cv2.IMWRITE_JPEG_QUALITY,88]); ok,enc=cv2.imencode('.jpg',ann,[cv2.IMWRITE_JPEG_QUALITY,88]); elapsed=int((datetime.now()-session_started).total_seconds()/60) if session_started else 0; sname=session_dir.name
    if ok: publish_binary(f'{BASE_TOPIC}/image/preview',enc.tobytes(),True)
    for k,v in [('growth',f'{growth:.1f}'),('height',height),('edge_y',edge),('frames',frame_no),('elapsed',elapsed),('status',status)]: publish(f'{BASE_TOPIC}/state/{k}',str(v),True)
    add_measurement(active_bake_id,sname,growth,height,edge,conf,status); publish_timelapse_state('recording',sname,frame_no)
    if int(cfg['timelapse_refresh_minutes'])>0 and time.time()-last_timelapse_build>=int(cfg['timelapse_refresh_minutes'])*60:
        try: build_timelapse()
        except: LOG.exception('Periodiskt timelapse-bygge misslyckades')

class Handler(BaseHTTPRequestHandler):
    def _allowed(self): return self.client_address[0] in {'172.30.32.2','127.0.0.1','::1'} or self.client_address[0].startswith('172.30.')
    def _json(self,status,obj):
        data=json.dumps(obj,ensure_ascii=False).encode(); self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
    def _body(self):
        n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
    def do_GET(self):
        if not self._allowed(): return self.send_error(403)
        path=urlparse(self.path).path.rstrip('/'); qs=parse_qs(urlparse(self.path).query)
        if path in {'','/'}:
            data=UI_PATH.read_bytes(); self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',str(len(data))); self.end_headers(); return self.wfile.write(data)
        if path.endswith('/api/config') or path=='/api/config': return self._json(200,{'camera_source':cfg.get('camera_source'),'camera_url':cfg.get('camera_url'),**current_roi(),'override_active':ROI_OVERRIDE_PATH.exists(),'active_bake_id':active_bake_id,'session_active':session_active})
        if path.endswith('/api/preview.jpg') or path=='/api/preview.jpg':
            try:
                img=fetch_frame(); ok,enc=cv2.imencode('.jpg',img,[cv2.IMWRITE_JPEG_QUALITY,88]); data=enc.tobytes(); self.send_response(200); self.send_header('Content-Type','image/jpeg'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(data))); self.end_headers(); return self.wfile.write(data)
            except Exception as e:
                LOG.exception("RTSP/Preview misslyckades")
                return self.send_error(502,str(e)) 
        if path.endswith('/api/bakes') or path=='/api/bakes': return self._json(200,list_bakes(min(int(qs.get('limit',['50'])[0]),200)))
        if '/api/bakes/' in path:
            bid=path.split('/api/bakes/',1)[1].split('/')[0]
            try:return self._json(200,bake_detail(bid))
            except KeyError:return self._json(404,{'error':'not_found'})
        return self.send_error(404)
    def do_POST(self):
        global active_bake_id
        if not self._allowed(): return self.send_error(403)
        path=urlparse(self.path).path.rstrip('/')
        try:
            if path.endswith('/api/roi') or path=='/api/roi': return self._json(200,save_roi_override(self._body()))
            if path.endswith('/api/bakes') or path=='/api/bakes': return self._json(201,create_bake(self._body()))
            if path.endswith('/api/active-bake') or path=='/api/active-bake':
                p=self._body(); set_active_bake(p.get('id')); return self._json(200,{'active_bake_id':active_bake_id})
            if '/api/bakes/' in path and '/phase/' in path:
                rest=path.split('/api/bakes/',1)[1]; bid,phase=rest.split('/phase/',1); return self._json(200,phase_bake(bid,phase,self._body()))
            if '/api/bakes/' in path and path.endswith('/clone'):
                bid=path.split('/api/bakes/',1)[1].rsplit('/clone',1)[0]; src=get_bake(bid); p=self._body(); return self._json(201,create_bake({'name':p.get('name') or src['name']+' kopia','recipe':src['recipe'],'bake':src['bake']}))
        except KeyError:return self._json(404,{'error':'not_found'})
        except Exception as e:return self._json(400,{'error':str(e)})
        return self.send_error(404)
    def do_PATCH(self):
        if not self._allowed(): return self.send_error(403)
        path=urlparse(self.path).path.rstrip('/')
        if '/api/bakes/' in path:
            bid=path.split('/api/bakes/',1)[1].split('/')[0]
            try:return self._json(200,update_bake(bid,self._body()))
            except KeyError:return self._json(404,{'error':'not_found'})
        return self.send_error(404)
    def do_DELETE(self):
        if not self._allowed():return self.send_error(403)
        path=urlparse(self.path).path.rstrip('/')
        if path.endswith('/api/roi') or path=='/api/roi': return self._json(200,clear_roi_override())
        return self.send_error(404)
    def log_message(self,fmt,*args): LOG.debug('Web UI: '+fmt,*args)

def start_web_ui():
    s=ThreadingHTTPServer(('0.0.0.0',8099),Handler); threading.Thread(target=s.serve_forever,daemon=True).start(); return s
def on_connect(client,userdata,flags,reason_code,properties=None): client.subscribe(f'{BASE_TOPIC}/cmd/#'); publish_discovery(); publish(f'{BASE_TOPIC}/state/active','ON' if session_active else 'OFF',True)
def on_message(client,userdata,msg):
    if msg.payload.decode(errors='replace')!='PRESS':return
    if msg.topic.endswith('/start'):threading.Thread(target=start_session,daemon=True).start()
    elif msg.topic.endswith('/stop'):threading.Thread(target=stop_session,daemon=True).start()
    elif msg.topic.endswith('/build'):threading.Thread(target=build_timelapse,daemon=True).start()
def setup_mqtt():
    global mqtt_client
    host,port,user,password,ssl=get_mqtt_service(); c=mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,client_id='sourdough-monitor',clean_session=True)
    if user:c.username_pw_set(user,password)
    if ssl:c.tls_set()
    c.will_set(f'{BASE_TOPIC}/availability','offline',retain=True); c.on_connect=on_connect; c.on_message=on_message; c.connect(host,port,60); c.loop_start(); mqtt_client=c

def main():
    global cfg,edge_history
    MEDIA_ROOT.mkdir(parents=True,exist_ok=True); init_db(); cfg=load_options(); cfg.update(load_roi_override()); edge_history=deque(maxlen=int(cfg['smoothing_frames'])); start_web_ui(); setup_mqtt(); publish(f'{BASE_TOPIC}/availability','online',True); LOG.info('Sourdough Monitor %s startad',VERSION)
    while True:
        try:
            if session_active: process_frame(); time.sleep(max(1,int(cfg['interval_seconds'])))
            else: time.sleep(1)
        except requests.RequestException: LOG.exception('Kamerafel'); publish(f'{BASE_TOPIC}/state/status','camera_error',True); time.sleep(10)
        except Exception: LOG.exception('Fel i övervakningsloopen'); publish(f'{BASE_TOPIC}/state/status','error',True); time.sleep(10)
if __name__=='__main__': main()
