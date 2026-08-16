# Copyright 2026 Arvid Sagbrant
# SPDX-License-Identifier: Apache-2.0

import hashlib, json, logging, os, re, shutil, sqlite3, statistics, subprocess, threading, time, uuid
from collections import deque
from contextlib import contextmanager
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit, urlparse, parse_qs

import cv2
import numpy as np
import paho.mqtt.client as mqtt
import requests

LOG = logging.getLogger('sourdough')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
LOG_LEVELS={'error':logging.ERROR,'info':logging.INFO,'debug':logging.DEBUG}

OPTIONS_PATH=Path('/data/options.json'); ROI_OVERRIDE_PATH=Path('/data/roi.json'); DETECTION_OVERRIDE_PATH=Path('/data/detection.json'); UI_PATH=Path('/app/ui.html')
MEDIA_ROOT=Path('/media/sourdough'); DB_PATH=Path('/data/sourdough_journal.db')
DEVICE_ID='sourdough_monitor'; BASE_TOPIC='sourdough_monitor'; DISCOVERY_PREFIX='homeassistant'; VERSION='0.9.2'
MAX_PHOTO_BYTES=15*1024*1024; MAX_PHOTO_EDGE=2560

class CameraError(RuntimeError): pass

session_lock=threading.Lock(); cfg_lock=threading.RLock(); photo_lock=threading.RLock()
session_active=False; session_dir=None; session_started=None; baseline_height_px=None; frame_no=0; last_timelapse_build=0.0; last_logged_status=None
edge_history=deque(maxlen=5); last_growth_values=deque(maxlen=10); peak_growth=-1.0; peak_frame_path=None; start_frame_path=None; last_frame_path=None
mqtt_client=None; cfg={}; active_bake_id=None; default_temperature_sensor_id=None

def configure_logging(value):
    name=str(value or 'info').strip().lower()
    if name not in LOG_LEVELS:
        LOG.warning('Okänd loggnivå %r; använder info',value); name='info'
    LOG.setLevel(LOG_LEVELS[name])
    return name

SCHEMA='''
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS bakes (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', started_at TEXT NOT NULL, finished_at TEXT,
 recipe_json TEXT NOT NULL DEFAULT '{}', process_json TEXT NOT NULL DEFAULT '{}', bake_json TEXT NOT NULL DEFAULT '{}',
 result_json TEXT NOT NULL DEFAULT '{}', media_json TEXT NOT NULL DEFAULT '{}', notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, bake_id TEXT NOT NULL, kind TEXT NOT NULL, ts TEXT NOT NULL, data_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS measurements (seq INTEGER PRIMARY KEY AUTOINCREMENT, bake_id TEXT NOT NULL, session TEXT, ts TEXT NOT NULL, growth REAL, height_px INTEGER, edge_y INTEGER, confidence REAL, status TEXT, temperature_c REAL, temperature_sensor TEXT);
CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS idx_events_bake_ts ON events(bake_id,ts);
CREATE INDEX IF NOT EXISTS idx_measurements_bake_ts ON measurements(bake_id,ts);
'''
JSON_COLS=('recipe','process','bake','result','media')
PHASES=(
    ('starter_used','starter_used_at','Degblandning'),
    ('bulk_start','bulk_start','Bulkjäsning'),
    ('bulk_end','bulk_end','Formning'),
    ('proof_start','proof_start','Slutjäsning'),
    ('proof_end','proof_end','Redo att baka'),
    ('baked','baked_at','Bakad'),
)

def now_iso(): return datetime.now().astimezone().isoformat(timespec='seconds')
@contextmanager
def db():
    c=sqlite3.connect(DB_PATH, timeout=10); c.row_factory=sqlite3.Row
    try: yield c; c.commit()
    finally: c.close()
def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.executescript(SCHEMA)
        columns={r['name'] for r in c.execute('PRAGMA table_info(measurements)').fetchall()}
        if 'temperature_c' not in columns: c.execute('ALTER TABLE measurements ADD COLUMN temperature_c REAL')
        if 'temperature_sensor' not in columns: c.execute('ALTER TABLE measurements ADD COLUMN temperature_sensor TEXT')
def load_active_bake():
    global active_bake_id
    with db() as c: row=c.execute("SELECT value FROM app_state WHERE key='active_bake_id'").fetchone()
    active_bake_id=row['value'] if row else None
    if active_bake_id:
        try: get_bake(active_bake_id)
        except KeyError: active_bake_id=None
def persist_active_bake():
    with db() as c:
        if active_bake_id is None: c.execute("DELETE FROM app_state WHERE key='active_bake_id'")
        else: c.execute("INSERT INTO app_state(key,value) VALUES('active_bake_id',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(active_bake_id,))
def load_default_temperature_sensor():
    global default_temperature_sensor_id
    with db() as c: row=c.execute("SELECT value FROM app_state WHERE key='default_temperature_sensor_id'").fetchone()
    default_temperature_sensor_id=row['value'] if row else None
def set_default_temperature_sensor(entity_id):
    global default_temperature_sensor_id
    entity_id=str(entity_id or '').strip() or None
    if entity_id and (not entity_id.startswith('sensor.') or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.' for c in entity_id)): raise ValueError('Ogiltigt sensor-id')
    default_temperature_sensor_id=entity_id
    with db() as c:
        if entity_id is None: c.execute("DELETE FROM app_state WHERE key='default_temperature_sensor_id'")
        else: c.execute("INSERT INTO app_state(key,value) VALUES('default_temperature_sensor_id',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(entity_id,))
    return default_temperature_sensor_id
def persist_session():
    if not session_active or not session_dir: return
    state={'dir':session_dir.name,'started_at':session_started.isoformat() if session_started else None,
           'baseline_height_px':baseline_height_px,'frame_no':frame_no,'peak_growth':peak_growth,
           'peak_frame':Path(peak_frame_path).name if peak_frame_path else None,
           'start_frame':Path(start_frame_path).name if start_frame_path else None,
           'last_frame':Path(last_frame_path).name if last_frame_path else None,
           'edge_history':list(edge_history),'last_growth_values':list(last_growth_values)}
    with db() as c: c.execute("INSERT INTO app_state(key,value) VALUES('monitor_session',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps(state),))
def clear_persisted_session():
    with db() as c: c.execute("DELETE FROM app_state WHERE key='monitor_session'")
def restore_session():
    global session_active,session_dir,session_started,baseline_height_px,frame_no,last_timelapse_build,peak_growth,peak_frame_path,start_frame_path,last_frame_path
    with db() as c: row=c.execute("SELECT value FROM app_state WHERE key='monitor_session'").fetchone()
    if not row: return False
    try:
        state=json.loads(row['value']); current=MEDIA_ROOT/state['dir']; frames_dir=current/'frames'
        if current.parent!=MEDIA_ROOT or not current.name.startswith('session_') or not frames_dir.is_dir(): raise ValueError('Ogiltig sessionskatalog')
        frames=sorted(frames_dir.glob('*.jpg')); names={p.name:p for p in frames}
        session_dir=current; session_started=datetime.fromisoformat(state['started_at']) if state.get('started_at') else datetime.now()
        baseline_height_px=state.get('baseline_height_px'); frame_no=max(int(state.get('frame_no') or 0),max((int(p.stem)+1 for p in frames if p.stem.isdigit()),default=0))
        peak_growth=float(state.get('peak_growth',-1.)); peak_frame_path=names.get(state.get('peak_frame'))
        start_frame_path=names.get(state.get('start_frame')) or (frames[0] if frames else None)
        last_frame_path=frames[-1] if frames else None
        edge_history.clear(); edge_history.extend(state.get('edge_history') or [])
        last_growth_values.clear(); last_growth_values.extend(state.get('last_growth_values') or [])
        session_active=True; last_timelapse_build=time.time()
        LOG.info('Återupptar session %s från bild %s',current.name,frame_no)
        return True
    except Exception:
        LOG.exception('Kunde inte återuppta sparad session'); clear_persisted_session(); return False
def decode_bake(r):
    d=dict(r)
    for k in JSON_COLS: d[k]=json.loads(d.pop(k+'_json') or '{}')
    return d
def bake_stage(bake):
    stage={'key':'planning','label':'Planering','changed_at':bake.get('started_at')}
    process=bake.get('process') or {}
    history=[]
    for phase,key,label in PHASES:
        if process.get(key):
            stage={'key':phase,'label':label,'changed_at':process[key]}
            history.append({**stage})
    return stage,history
def enrich_bake(bake):
    stage,history=bake_stage(bake); bake['stage']=stage; bake['stage_history']=history; return bake
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
    return [enrich_bake(decode_bake(r)) for r in rows]
def create_bake(p):
    global active_bake_id
    ts=now_iso(); bid=p.get('id') or next_bake_id(); name=(p.get('name') or 'Nytt surdegsbröd').strip()
    payload=dict(p); process=dict(payload.get('process') or {})
    if 'temperature_sensor_entity_id' not in process and default_temperature_sensor_id: process['temperature_sensor_entity_id']=default_temperature_sensor_id
    payload['process']=process; vals=[json.dumps(payload.get(k) or {},ensure_ascii=False) for k in JSON_COLS]
    with db() as c:
        c.execute('''INSERT INTO bakes(id,name,status,started_at,recipe_json,process_json,bake_json,result_json,media_json,notes,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',(bid,name,p.get('status','active'),p.get('started_at') or ts,*vals,p.get('notes',''),ts,ts))
    active_bake_id=bid; persist_active_bake(); add_event(bid,'created',{'name':name}); publish_journal_state(); LOG.info('Bak skapat och aktiverat: id=%s namn=%s',bid,name); return get_bake(bid)
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
    keys={phase:key for phase,key,_ in PHASES}
    if phase not in keys: raise ValueError('Okänd fas')
    patch={'process':{keys[phase]:now_iso(), **(data or {})}}
    if phase=='baked': patch.update(status='finished',finished_at=now_iso())
    out=update_bake(bid,patch); add_event(bid,phase,data or {}); LOG.info('Bakfas uppdaterad: bak=%s fas=%s',bid,phase); return out
def set_active_bake(bid):
    global active_bake_id
    if bid is not None: get_bake(bid)
    active_bake_id=bid; persist_active_bake(); publish_journal_state(); LOG.info('Aktivt bak ändrat: bak=%s',bid or 'inget')
def add_measurement(bid,session,growth,height,edge,conf,status,temperature=None,temperature_sensor=None):
    if not bid: return
    with db() as c: c.execute('INSERT INTO measurements(bake_id,session,ts,growth,height_px,edge_y,confidence,status,temperature_c,temperature_sensor) VALUES(?,?,?,?,?,?,?,?,?,?)',(bid,session,now_iso(),growth,height,edge,conf,status,temperature,temperature_sensor))
def bake_detail(bid):
    out=enrich_bake(get_bake(bid))
    with db() as c:
        out['events']=[dict(r) for r in c.execute('SELECT seq,kind,ts,data_json FROM events WHERE bake_id=? ORDER BY ts',(bid,)).fetchall()]
        for e in out['events']: e['data']=json.loads(e.pop('data_json') or '{}')
        m=c.execute('SELECT COUNT(*) n, MIN(growth) min_growth, MAX(growth) max_growth, AVG(growth) avg_growth, COUNT(temperature_c) temperature_n, MIN(temperature_c) min_temperature_c, MAX(temperature_c) max_temperature_c, AVG(temperature_c) avg_temperature_c, (SELECT temperature_c FROM measurements latest WHERE latest.bake_id=? AND temperature_c IS NOT NULL ORDER BY ts DESC, seq DESC LIMIT 1) latest_temperature_c, (SELECT ts FROM measurements latest WHERE latest.bake_id=? AND temperature_c IS NOT NULL ORDER BY ts DESC, seq DESC LIMIT 1) latest_temperature_at FROM measurements WHERE bake_id=?',(bid,bid,bid)).fetchone()
        out['measurement_summary']=dict(m)
    sensor=(out.get('process') or {}).get('temperature_sensor_entity_id')
    if sensor:
        try: out['temperature']=read_temperature_sensor(sensor)
        except Exception as e: out['temperature']={'entity_id':sensor,'available':False,'error':str(e)}
    for photo in out.get('media',{}).get('photos',[]):
        photo['url']=f"api/bakes/{quote(bid,safe='')}/photos/{quote(str(photo.get('id','')),safe='')}"
    return out

def bake_photo_dir(bid):
    return MEDIA_ROOT/'bakes'/hashlib.sha256(bid.encode()).hexdigest()[:20]
def photo_path(bid,photo_id):
    if not photo_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-' for c in photo_id): raise ValueError('Ogiltigt foto-id')
    return bake_photo_dir(bid)/(photo_id+'.jpg')
def add_bake_photo(bid,data,original_name='',caption='',featured=False):
    if not data: raise ValueError('Bilden är tom')
    if len(data)>MAX_PHOTO_BYTES: raise ValueError('Bilden är för stor (max 15 MB)')
    get_bake(bid); img=cv2.imdecode(np.frombuffer(data,np.uint8),cv2.IMREAD_COLOR)
    if img is None: raise ValueError('Filen kunde inte läsas som en bild')
    h,w=img.shape[:2]; scale=min(1.0,MAX_PHOTO_EDGE/max(h,w))
    if scale<1: img=cv2.resize(img,(max(1,round(w*scale)),max(1,round(h*scale))),interpolation=cv2.INTER_AREA)
    h,w=img.shape[:2]; photo_id=datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]
    folder=bake_photo_dir(bid); folder.mkdir(parents=True,exist_ok=True); dest=photo_path(bid,photo_id); tmp=dest.with_suffix('.tmp.jpg')
    if not cv2.imwrite(str(tmp),img,[cv2.IMWRITE_JPEG_QUALITY,88]): raise RuntimeError('Bilden kunde inte sparas')
    tmp.replace(dest)
    photo={'id':photo_id,'filename':original_name[:200] or 'photo.jpg','caption':caption[:500],'featured':bool(featured),'uploaded_at':now_iso(),'width':w,'height':h,'media_content_id':media_uri(dest)}
    try:
        with photo_lock:
            bake=get_bake(bid); photos=[dict(p) for p in bake.get('media',{}).get('photos',[]) if isinstance(p,dict)]
            if featured:
                for p in photos: p['featured']=False
            photos.append(photo); update_bake(bid,{'media':{'photos':photos}})
    except Exception:
        dest.unlink(missing_ok=True); raise
    add_event(bid,'photo_added',{'id':photo_id,'filename':photo['filename'],'featured':photo['featured']}); LOG.info('Foto tillagt: bak=%s foto=%s featured=%s',bid,photo_id,photo['featured'])
    return {**photo,'url':f"api/bakes/{quote(bid,safe='')}/photos/{quote(photo_id,safe='')}"}
def set_featured_photo(bid,photo_id):
    with photo_lock:
        bake=get_bake(bid); photos=[dict(p) for p in bake.get('media',{}).get('photos',[]) if isinstance(p,dict)]
        if not any(p.get('id')==photo_id for p in photos): raise KeyError(photo_id)
        for p in photos: p['featured']=p.get('id')==photo_id
        update_bake(bid,{'media':{'photos':photos}})
    add_event(bid,'photo_featured',{'id':photo_id}); LOG.info('Utvalt foto ändrat: bak=%s foto=%s',bid,photo_id); return bake_detail(bid)
def delete_bake_photo(bid,photo_id):
    with photo_lock:
        bake=get_bake(bid); photos=[dict(p) for p in bake.get('media',{}).get('photos',[]) if isinstance(p,dict)]
        if not any(p.get('id')==photo_id for p in photos): raise KeyError(photo_id)
        update_bake(bid,{'media':{'photos':[p for p in photos if p.get('id')!=photo_id]}})
        try: photo_path(bid,photo_id).unlink(missing_ok=True)
        except OSError: LOG.exception('Kunde inte radera bildfil för %s',photo_id)
    add_event(bid,'photo_deleted',{'id':photo_id}); LOG.info('Foto raderat: bak=%s foto=%s',bid,photo_id)

DETECTION_DEFAULTS={'blur_kernel':7,'sobel_kernel':3,'row_smoothing':9,'search_top_pct':5,'search_bottom_pct':95,'polarity':'both','candidate_count':4,'max_jump_pct':8}
DETECTION_KEYS=set(DETECTION_DEFAULTS)

def load_options(): return json.loads(OPTIONS_PATH.read_text())
def validate_detection(v):
    out={}
    for key,lo,hi in [('blur_kernel',1,31),('sobel_kernel',1,7),('row_smoothing',1,51),('search_top_pct',0,90),('search_bottom_pct',10,100),('candidate_count',1,8),('max_jump_pct',0,50)]:
        value=int(round(float(v.get(key,DETECTION_DEFAULTS[key]))))
        if not lo<=value<=hi: raise ValueError(f'Ogiltigt värde för {key}')
        if key in {'blur_kernel','sobel_kernel','row_smoothing'} and value%2==0: value+=1
        out[key]=min(value,hi)
    out['polarity']=str(v.get('polarity','both'))
    if out['polarity'] not in {'both','dark_to_light','light_to_dark'}: raise ValueError('Ogiltig kantriktning')
    if out['search_top_pct']>=out['search_bottom_pct']: raise ValueError('Sökintervallets början måste vara före slutet')
    return out
def load_detection_override():
    try: return validate_detection(json.loads(DETECTION_OVERRIDE_PATH.read_text())) if DETECTION_OVERRIDE_PATH.exists() else {}
    except: LOG.exception('Kunde inte läsa detektionsinställningar'); return {}
def current_detection():
    with cfg_lock: return {k:cfg.get(k,DETECTION_DEFAULTS[k]) for k in DETECTION_DEFAULTS}
def save_detection_override(v):
    clean=validate_detection(v); tmp=DETECTION_OVERRIDE_PATH.with_suffix('.tmp'); tmp.write_text(json.dumps(clean,indent=2)); tmp.replace(DETECTION_OVERRIDE_PATH)
    with cfg_lock: cfg.update(clean)
    LOG.info('Detektionsinställningar sparade')
    return clean
def clear_detection_override():
    if DETECTION_OVERRIDE_PATH.exists(): DETECTION_OVERRIDE_PATH.unlink()
    with cfg_lock: cfg.update(DETECTION_DEFAULTS)
    LOG.info('Detektionsinställningar återställda')
    return current_detection()
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
    LOG.info('ROI sparad: x=%s y=%s bredd=%s höjd=%s',clean['roi_x_pct'],clean['roi_y_pct'],clean['roi_width_pct'],clean['roi_height_pct'])
    return clean
def clear_roi_override():
    if ROI_OVERRIDE_PATH.exists(): ROI_OVERRIDE_PATH.unlink()
    base=load_options()
    with cfg_lock:
        for k in ('roi_x_pct','roi_y_pct','roi_width_pct','roi_height_pct'): cfg[k]=base[k]
    LOG.info('ROI återställd till add-on-konfigurationen')
    return current_roi()

def supervisor_mqtt_service():
    token=os.environ.get('SUPERVISOR_TOKEN')
    if not token: raise RuntimeError('SUPERVISOR_TOKEN saknas')
    r=requests.get('http://supervisor/services/mqtt',headers={'Authorization':f'Bearer {token}'},timeout=10)
    r.raise_for_status(); body=r.json()
    if body.get('result')!='ok': raise RuntimeError(body.get('message') or 'MQTT-tjänsten kunde inte hämtas')
    data=body.get('data') or {}
    return str(data['host']),int(data.get('port',1883)),str(data.get('username','') or ''),str(data.get('password','') or ''),bool(data.get('ssl',False))
def home_assistant_headers():
    token=os.environ.get('SUPERVISOR_TOKEN')
    if not token: raise RuntimeError('SUPERVISOR_TOKEN saknas')
    return {'Authorization':f'Bearer {token}','Content-Type':'application/json'}
def temperature_c(value,unit):
    value=float(value); normalized=str(unit or '').strip().replace('°','').upper()
    if normalized in {'F','℉'}: return (value-32)*5/9
    if normalized=='K': return value-273.15
    return value
def temperature_sensor_from_state(state):
    entity_id=str(state.get('entity_id') or ''); attributes=state.get('attributes') or {}; unit=attributes.get('unit_of_measurement') or ''
    if not entity_id.startswith('sensor.') or attributes.get('device_class')!='temperature': raise ValueError('Entiteten är inte en temperatursensor')
    try: value=round(temperature_c(state.get('state'),unit),2)
    except (TypeError,ValueError): raise ValueError('Temperatursensorn saknar ett numeriskt värde')
    return {'entity_id':entity_id,'name':attributes.get('friendly_name') or entity_id,'temperature_c':value,'source_value':state.get('state'),'source_unit':unit,'updated_at':state.get('last_updated') or state.get('last_changed'),'available':True}
def list_temperature_sensors():
    r=requests.get('http://supervisor/core/api/states',headers=home_assistant_headers(),timeout=10); r.raise_for_status(); sensors=[]
    for state in r.json():
        try: sensors.append(temperature_sensor_from_state(state))
        except ValueError: pass
    return sorted(sensors,key=lambda sensor:sensor['name'].casefold())
def read_temperature_sensor(entity_id):
    entity_id=str(entity_id or '').strip()
    if not entity_id.startswith('sensor.') or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.' for c in entity_id): raise ValueError('Ogiltigt sensor-id')
    r=requests.get('http://supervisor/core/api/states/'+quote(entity_id,safe=''),headers=home_assistant_headers(),timeout=10); r.raise_for_status()
    return temperature_sensor_from_state(r.json())
def bake_temperature(bid):
    if not bid: return None
    sensor=(get_bake(bid).get('process') or {}).get('temperature_sensor_entity_id')
    if not sensor: return None
    return read_temperature_sensor(sensor)
def get_mqtt_service():
    host=str(cfg.get('mqtt_host','') or '').strip()
    if not host: return supervisor_mqtt_service()
    return host,int(cfg.get('mqtt_port',1883)),str(cfg.get('mqtt_username','') or ''),str(cfg.get('mqtt_password','') or ''),bool(cfg.get('mqtt_tls',False))
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
        payload={'name':name,'unique_id':f'{DEVICE_ID}_{oid}','state_topic':f'{BASE_TOPIC}/state/{oid}','availability_topic':av,'device':dev,'icon':icon}
        if oid=='active_bake': payload['json_attributes_topic']=f'{BASE_TOPIC}/attributes/{oid}'
        publish(f'{DISCOVERY_PREFIX}/sensor/{DEVICE_ID}/{oid}/config',payload,True)
    publish(f'{DISCOVERY_PREFIX}/image/{DEVICE_ID}/preview/config',{'name':'Surdeg preview','unique_id':f'{DEVICE_ID}_preview','image_topic':f'{BASE_TOPIC}/image/preview','content_type':'image/jpeg','availability_topic':av,'device':dev},True)
    publish(f'{DISCOVERY_PREFIX}/sensor/{DEVICE_ID}/timelapse/config',{'name':'Surdeg timelapse','unique_id':f'{DEVICE_ID}_timelapse','state_topic':f'{BASE_TOPIC}/state/timelapse','json_attributes_topic':f'{BASE_TOPIC}/attributes/timelapse','availability_topic':av,'device':dev,'icon':'mdi:movie-open'},True)
    publish(f'{DISCOVERY_PREFIX}/binary_sensor/{DEVICE_ID}/active/config',{'name':'Surdeg övervakning','unique_id':f'{DEVICE_ID}_active','state_topic':f'{BASE_TOPIC}/state/active','payload_on':'ON','payload_off':'OFF','availability_topic':av,'device':dev,'icon':'mdi:camera-timer'},True)
    for oid,name,icon in [('start','Starta surdeg','mdi:play'),('stop','Stoppa och bygg timelapse','mdi:stop'),('build','Bygg timelapse nu','mdi:movie-open-plus')]: publish(f'{DISCOVERY_PREFIX}/button/{DEVICE_ID}/{oid}/config',{'name':name,'unique_id':f'{DEVICE_ID}_{oid}','command_topic':f'{BASE_TOPIC}/cmd/{oid}','payload_press':'PRESS','availability_topic':av,'device':dev,'icon':icon},True)
    publish(av,'online',True); publish_journal_state()
def publish_journal_state():
    if active_bake_id:
        try:
            b=get_bake(active_bake_id); stage,_=bake_stage(b); process=b.get('process') or {}
            publish(f'{BASE_TOPIC}/state/active_bake',b['name'],True); publish(f'{BASE_TOPIC}/attributes/active_bake',{'id':b['id'],'status':b['status'],'started_at':b['started_at'],'stage':stage['label'],'stage_key':stage['key'],'stage_changed_at':stage['changed_at'],'temperature_sensor_entity_id':process.get('temperature_sensor_entity_id')},True); return
        except KeyError: pass
    publish(f'{BASE_TOPIC}/state/active_bake','none',True); publish(f'{BASE_TOPIC}/attributes/active_bake',{},True)

def camera_url_with_credentials(url):
    u=str(cfg.get('camera_username','') or ''); p=str(cfg.get('camera_password','') or '')
    if not u:return url
    parts=urlsplit(url)
    if '@' in parts.netloc:return url
    auth=quote(u,safe='')+(':'+quote(p,safe='') if p else '')
    return urlunsplit((parts.scheme,f'{auth}@{parts.netloc}',parts.path,parts.query,parts.fragment))
def safe_camera_error(value):
    text=re.sub(r'(?i)([a-z][a-z0-9+.-]*://)[^\s/@]+@',r'\1***@',str(value))
    password=str(cfg.get('camera_password','') or '')
    return text.replace(password,'***') if password else text
def fetch_snapshot_frame():
    LOG.debug('Hämtar snapshot-bildruta')
    try:
        auth=(cfg['camera_username'],cfg.get('camera_password','')) if cfg.get('camera_username') else None; r=requests.get(cfg['camera_url'],auth=auth,timeout=15); r.raise_for_status(); img=cv2.imdecode(np.frombuffer(r.content,np.uint8),cv2.IMREAD_COLOR)
    except requests.RequestException as e: raise CameraError(safe_camera_error(e)) from None
    if img is None: raise CameraError('Snapshot kunde inte avkodas')
    LOG.debug('Snapshot-bildruta hämtad: bredd=%s höjd=%s',img.shape[1],img.shape[0])
    return img
def fetch_rtsp_frame():
    timeout=max(5,int(cfg.get('camera_timeout_seconds',20))); attempts=1+max(0,int(cfg.get('camera_retries',1)))
    cmd=['ffmpeg','-hide_banner','-loglevel','error','-rtsp_transport',str(cfg.get('rtsp_transport','tcp')),'-i',camera_url_with_credentials(cfg['camera_url']),'-map','0:v:0','-frames:v','1','-f','image2pipe','-vcodec','mjpeg','pipe:1']
    last_error='okänt kamerafel'
    for attempt in range(1,attempts+1):
        started=time.monotonic(); LOG.debug('Hämtar RTSP-bildruta: transport=%s försök=%s/%s timeout=%ss',cfg.get('rtsp_transport','tcp'),attempt,attempts,timeout)
        try:
            p=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout)
            if p.returncode or not p.stdout:
                detail=safe_camera_error(p.stderr.decode(errors='replace').strip())
                last_error=('ffmpeg avslutades utan en bildruta'+(f': {detail[:500]}' if detail else ''))
            else:
                img=cv2.imdecode(np.frombuffer(p.stdout,np.uint8),cv2.IMREAD_COLOR)
                if img is not None:
                    LOG.debug('RTSP-bildruta hämtad: bredd=%s höjd=%s tid=%.2fs',img.shape[1],img.shape[0],time.monotonic()-started); return img
                last_error='RTSP-bildrutan kunde inte avkodas'
        except subprocess.TimeoutExpired:
            last_error=f'RTSP-kameran svarade inte inom {timeout} sekunder'
        except OSError as e:
            last_error=f'ffmpeg kunde inte startas: {safe_camera_error(e)}'
        if attempt<attempts:
            LOG.warning('RTSP-hämtning misslyckades (försök %s/%s): %s; försöker igen',attempt,attempts,last_error)
            time.sleep(2)
    raise CameraError(f'{last_error} efter {attempts} försök')
def fetch_frame(): return fetch_snapshot_frame() if str(cfg.get('camera_source','snapshot')).lower()=='snapshot' else fetch_rtsp_frame()
def roi_from_image(img):
    h,w=img.shape[:2]; r=current_roi(); x=int(w*r['roi_x_pct']/100); y=int(h*r['roi_y_pct']/100); x2=min(w,x+int(w*r['roi_width_pct']/100)); y2=min(h,y+int(h*r['roi_height_pct']/100)); return x,y,x2,y2
def detect_top(img, params=None, update_history=True):
    p={**current_detection(),**(params or {})}; x1,y1,x2,y2=roi_from_image(img); crop=img[y1:y2,x1:x2]
    gray=cv2.cvtColor(crop,cv2.COLOR_BGR2GRAY); blur=int(p['blur_kernel'])
    if blur>1: gray=cv2.GaussianBlur(gray,(blur,blur),0)
    grad=cv2.Sobel(gray,cv2.CV_32F,0,1,ksize=int(p['sobel_kernel']))
    if p['polarity']=='dark_to_light': grad=np.maximum(grad,0)
    elif p['polarity']=='light_to_dark': grad=np.maximum(-grad,0)
    else: grad=np.abs(grad)
    # Median across the width rejects narrow reflections and vertical jar details.
    score=np.percentile(grad,65,axis=1); smooth=int(p['row_smoothing'])
    if smooth>1: score=cv2.GaussianBlur(score.reshape(-1,1),(1,smooth),0).ravel()
    start=max(0,int(len(score)*int(p['search_top_pct'])/100)); stop=min(len(score),int(len(score)*int(p['search_bottom_pct'])/100))
    # Once a session has a stable edge, only accept physically plausible movement.
    # Expressing the limit as a percentage keeps it independent of camera resolution.
    continuity_start,continuity_stop=start,stop
    if update_history and edge_history and int(p['max_jump_pct'])>0:
        previous=int(statistics.median(edge_history))-y1
        max_jump=max(1,int(len(score)*int(p['max_jump_pct'])/100))
        continuity_start=max(start,previous-max_jump); continuity_stop=min(stop,previous+max_jump+1)
    usable=score[continuity_start:continuity_stop]
    if not len(usable): raise ValueError('Tomt sökintervall')
    ly=int(np.argmax(usable))+continuity_start; conf=float(score[ly]/(np.mean(usable)+1e-6)); gy=y1+ly
    if update_history: edge_history.append(gy); sy=int(statistics.median(edge_history))
    else: sy=gy
    order=np.argsort(usable)[::-1]; candidates=[]
    for index in order:
        row=int(index)+continuity_start
        if all(abs(row-old)>=max(3,smooth//2) for old in candidates): candidates.append(row)
        if len(candidates)>=int(p['candidate_count']): break
    return sy,max(1,y2-sy),conf,(x1,y1,x2,y2),[y1+r for r in candidates]
def draw_contrast_line(img,start,end,color,thickness=1):
    cv2.line(img,start,end,(0,0,0),thickness+4,cv2.LINE_AA)
    cv2.line(img,start,end,color,thickness,cv2.LINE_AA)
def draw_contrast_rectangle(img,start,end,color,thickness=1):
    cv2.rectangle(img,start,end,(0,0,0),thickness+4,cv2.LINE_AA)
    cv2.rectangle(img,start,end,color,thickness,cv2.LINE_AA)
def draw_contrast_text(img,text,origin,color,font_scale=.8,thickness=2):
    font=cv2.FONT_HERSHEY_SIMPLEX; (width,height),baseline=cv2.getTextSize(text,font,font_scale,thickness); x,y=origin; padding=6
    cv2.rectangle(img,(x-padding,y-height-padding),(x+width+padding,y+baseline+padding),(0,0,0),cv2.FILLED)
    cv2.putText(img,text,(x,y),font,font_scale,color,thickness,cv2.LINE_AA)
def annotate(img,edge,roi,growth,conf,status):
    x1,y1,x2,y2=roi; out=img.copy(); draw_contrast_rectangle(out,(x1,y1),(x2,y2),(255,255,255),2); draw_contrast_line(out,(x1,edge),(x2,edge),(255,255,255),3); draw_contrast_text(out,f'{growth:.1f}%  conf {conf:.2f}  {status}',(x1,max(30,y1-10)),(255,255,255)); return out
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
def annotate_detection(img,edge,roi,conf,candidates,params):
    x1,y1,x2,y2=roi; out=img.copy(); draw_contrast_rectangle(out,(x1,y1),(x2,y2),(0,229,255),2)
    for y in candidates[1:]: draw_contrast_line(out,(x1,y),(x2,y),(0,140,255),1)
    draw_contrast_line(out,(x1,edge),(x2,edge),(80,255,80),3)
    top=y1+int((y2-y1)*int(params['search_top_pct'])/100); bottom=y1+int((y2-y1)*int(params['search_bottom_pct'])/100)
    draw_contrast_line(out,(x1,top),(x2,top),(255,120,80),1); draw_contrast_line(out,(x1,bottom),(x2,bottom),(255,120,80),1)
    draw_contrast_text(out,f'VALD KANT y={edge}  konfidens={conf:.2f}',(x1,max(28,y1-10)),(80,255,80),.65,2)
    return out
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
    if not current: LOG.debug('Timelapse hoppas över: ingen session'); return
    frames=sorted((current/'frames').glob('*.jpg'))
    if len(frames)<2: LOG.debug('Timelapse hoppas över: session=%s bilder=%s',current.name,len(frames)); return
    out=current/'timelapse.mp4'; tmp=current/'timelapse.tmp.mp4'; fps=int(cfg['timelapse_fps']); LOG.info('Bygger timelapse: session=%s bilder=%s fps=%s',current.name,len(frames),fps); subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-framerate',str(fps),'-i',str(current/'frames'/'%06d.jpg'),'-c:v','libx264','-preset','veryfast','-crf','22','-pix_fmt','yuv420p','-movflags','+faststart',str(tmp)],check=True); tmp.replace(out); latest=MEDIA_ROOT/'latest.mp4'; shutil.copy2(out,latest); publish_timelapse_state('ready',current.name,len(frames),len(frames)/max(1,fps),latest); last_timelapse_build=time.time(); LOG.info('Timelapse klar: session=%s fil=%s',current.name,out.name)
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
    for old in sessions[int(cfg['keep_sessions']):]: shutil.rmtree(old,ignore_errors=True); LOG.info('Gammal session raderad: session=%s',old.name)
def start_session():
    global session_active,session_dir,session_started,baseline_height_px,frame_no,last_timelapse_build,peak_growth,peak_frame_path,start_frame_path,last_frame_path,last_logged_status
    with session_lock:
        if session_active: LOG.debug('Startkommando ignorerat: en session är redan aktiv'); return
        session_dir=MEDIA_ROOT/('session_'+datetime.now().strftime('%Y%m%d-%H%M%S')); (session_dir/'frames').mkdir(parents=True,exist_ok=True); session_started=datetime.now(); baseline_height_px=None; frame_no=0; edge_history.clear(); last_growth_values.clear(); peak_growth=-1.; peak_frame_path=start_frame_path=last_frame_path=None; last_logged_status=None; session_active=True; last_timelapse_build=time.time(); persist_session()
    if active_bake_id: update_bake(active_bake_id,{'media':{'session':session_dir.name},'process':{'starter_monitor_started_at':now_iso()}})
    publish(f'{BASE_TOPIC}/state/active','ON',True); publish(f'{BASE_TOPIC}/state/session',session_dir.name,True); publish(f'{BASE_TOPIC}/state/status','calibrating',True); publish_timelapse_state('recording',session_dir.name,0); LOG.info('Övervakningssession startad: session=%s aktivt_bak=%s',session_dir.name,active_bake_id or 'inget'); prune_sessions()
def stop_session():
    global session_active,last_logged_status
    with session_lock: was=session_active; session_active=False; name=session_dir.name if session_dir else None; frames=frame_no; clear_persisted_session()
    if was:
        publish_timelapse_state('building',name,frames)
        try: build_timelapse(); finalize_keyframes_and_cleanup(); publish(f'{BASE_TOPIC}/state/status','stopped',True); LOG.info('Övervakningssession stoppad: session=%s bilder=%s',name,frames)
        except Exception: LOG.exception('Timelapse-bygge misslyckades'); publish_timelapse_state('error',name,frames)
    else: LOG.debug('Stoppkommando ignorerat: ingen session är aktiv')
    last_logged_status=None; publish(f'{BASE_TOPIC}/state/active','OFF',True)
def process_frame():
    global frame_no,baseline_height_px,peak_growth,peak_frame_path,start_frame_path,last_frame_path,last_logged_status
    img=fetch_frame(); edge,height,conf,roi,_=detect_top(img)
    with session_lock:
        if baseline_height_px is None: baseline_height_px=height
        growth=100.*height/max(1,baseline_height_px); status=infer_status(growth); ann=annotate(img,edge,roi,growth,conf,status); fp=session_dir/'frames'/f'{frame_no:06d}.jpg'; cv2.imwrite(str(fp),ann,[cv2.IMWRITE_JPEG_QUALITY,88])
        if frame_no==0:start_frame_path=fp
        if growth>peak_growth: peak_growth=growth; peak_frame_path=fp
        last_frame_path=fp; frame_no+=1; persist_session(); cv2.imwrite(str(MEDIA_ROOT/'latest.jpg'),ann,[cv2.IMWRITE_JPEG_QUALITY,88]); ok,enc=cv2.imencode('.jpg',ann,[cv2.IMWRITE_JPEG_QUALITY,88]); elapsed=int((datetime.now()-session_started).total_seconds()/60) if session_started else 0; sname=session_dir.name
    if ok: publish_binary(f'{BASE_TOPIC}/image/preview',enc.tobytes(),True)
    for k,v in [('growth',f'{growth:.1f}'),('height',height),('edge_y',edge),('frames',frame_no),('elapsed',elapsed),('status',status)]: publish(f'{BASE_TOPIC}/state/{k}',str(v),True)
    bid=active_bake_id; reading=None
    try: reading=bake_temperature(bid)
    except Exception: LOG.exception('Kunde inte läsa temperatursensorn för %s',bid)
    if status!=last_logged_status: LOG.info('Surdegsstatus ändrad: session=%s status=%s tillväxt=%.1f%%',sname,status,growth); last_logged_status=status
    LOG.debug('Bildruta bearbetad: session=%s bild=%s tillväxt=%.1f%% höjd=%spx kant_y=%s konfidens=%.2f temperatur=%s',sname,frame_no,growth,height,edge,conf,reading['temperature_c'] if reading else 'saknas')
    add_measurement(bid,sname,growth,height,edge,conf,status,reading and reading['temperature_c'],reading and reading['entity_id']); publish_timelapse_state('recording',sname,frame_no)
    if int(cfg['timelapse_refresh_minutes'])>0 and time.time()-last_timelapse_build>=int(cfg['timelapse_refresh_minutes'])*60:
        try: build_timelapse()
        except: LOG.exception('Periodiskt timelapse-bygge misslyckades')

class Handler(BaseHTTPRequestHandler):
    def _allowed(self): return self.client_address[0] in {'172.30.32.2','127.0.0.1','::1'} or self.client_address[0].startswith('172.30.')
    def _json(self,status,obj):
        data=json.dumps(obj,ensure_ascii=False).encode(); self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
    def _body(self):
        n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
    def _photo(self,bid,photo_id):
        bake=get_bake(bid)
        if not any(isinstance(p,dict) and p.get('id')==photo_id for p in bake.get('media',{}).get('photos',[])): raise KeyError(photo_id)
        data=photo_path(bid,photo_id).read_bytes(); self.send_response(200); self.send_header('Content-Type','image/jpeg'); self.send_header('Cache-Control','private, max-age=86400'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        if not self._allowed(): return self.send_error(403)
        path=urlparse(self.path).path.rstrip('/'); qs=parse_qs(urlparse(self.path).query)
        if path in {'','/'}:
            data=UI_PATH.read_bytes(); self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',str(len(data))); self.end_headers(); return self.wfile.write(data)
        if path.endswith('/api/config') or path=='/api/config': return self._json(200,{'camera_source':cfg.get('camera_source'),'camera_url':cfg.get('camera_url'),**current_roi(),'override_active':ROI_OVERRIDE_PATH.exists(),'active_bake_id':active_bake_id,'session_active':session_active,'default_temperature_sensor_entity_id':default_temperature_sensor_id})
        if path.endswith('/api/temperature-sensors') or path=='/api/temperature-sensors':
            try:return self._json(200,list_temperature_sensors())
            except Exception as e: LOG.exception('Kunde inte hämta temperatursensorer'); return self._json(502,{'error':str(e)})
        if path.endswith('/api/detection') or path=='/api/detection': return self._json(200,{**current_detection(),'override_active':DETECTION_OVERRIDE_PATH.exists()})
        if path.endswith('/api/detection-preview.jpg') or path=='/api/detection-preview.jpg':
            try:
                raw={k:qs[k][0] for k in DETECTION_KEYS if k in qs}; params=validate_detection({**current_detection(),**raw}); img=fetch_frame(); edge,_,conf,roi,candidates=detect_top(img,params,False); ann=annotate_detection(img,edge,roi,conf,candidates,params); ok,enc=cv2.imencode('.jpg',ann,[cv2.IMWRITE_JPEG_QUALITY,90]); data=enc.tobytes(); self.send_response(200); self.send_header('Content-Type','image/jpeg'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(data))); self.end_headers(); return self.wfile.write(data)
            except Exception as e: LOG.exception('Detektionspreview misslyckades'); return self.send_error(502,str(e))
        if path.endswith('/api/preview.jpg') or path=='/api/preview.jpg':
            try:
                img=fetch_frame(); ok,enc=cv2.imencode('.jpg',img,[cv2.IMWRITE_JPEG_QUALITY,88]); data=enc.tobytes(); self.send_response(200); self.send_header('Content-Type','image/jpeg'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(data))); self.end_headers(); return self.wfile.write(data)
            except Exception as e:
                LOG.exception("RTSP/Preview misslyckades")
                return self.send_error(502,str(e)) 
        if path.endswith('/api/bakes') or path=='/api/bakes': return self._json(200,list_bakes(min(int(qs.get('limit',['50'])[0]),200)))
        if '/api/bakes/' in path and '/photos/' in path:
            rest=path.split('/api/bakes/',1)[1]; bid,photo_id=map(unquote,rest.split('/photos/',1))
            try:return self._photo(bid,photo_id)
            except (KeyError,FileNotFoundError,ValueError):return self._json(404,{'error':'not_found'})
        if '/api/bakes/' in path:
            bid=unquote(path.split('/api/bakes/',1)[1].split('/')[0])
            try:return self._json(200,bake_detail(bid))
            except KeyError:return self._json(404,{'error':'not_found'})
        return self.send_error(404)
    def do_POST(self):
        global active_bake_id
        if not self._allowed(): return self.send_error(403)
        path=urlparse(self.path).path.rstrip('/')
        try:
            if path.endswith('/api/roi') or path=='/api/roi': return self._json(200,save_roi_override(self._body()))
            if path.endswith('/api/detection') or path=='/api/detection': return self._json(200,save_detection_override(self._body()))
            if path.endswith('/api/default-temperature-sensor') or path=='/api/default-temperature-sensor': return self._json(200,{'default_temperature_sensor_entity_id':set_default_temperature_sensor(self._body().get('id'))})
            if path.endswith('/api/bakes') or path=='/api/bakes': return self._json(201,create_bake(self._body()))
            if '/api/bakes/' in path and path.endswith('/photos'):
                bid=unquote(path.split('/api/bakes/',1)[1].rsplit('/photos',1)[0]); n=int(self.headers.get('Content-Length','0'))
                if n>MAX_PHOTO_BYTES:return self._json(413,{'error':'Bilden är för stor (max 15 MB)'})
                data=self.rfile.read(n); name=unquote(self.headers.get('X-Photo-Filename','photo.jpg')); caption=unquote(self.headers.get('X-Photo-Caption','')); featured=self.headers.get('X-Photo-Featured','false').lower()=='true'
                return self._json(201,add_bake_photo(bid,data,name,caption,featured))
            if '/api/bakes/' in path and '/photos/' in path and path.endswith('/featured'):
                rest=path.split('/api/bakes/',1)[1].rsplit('/featured',1)[0]; bid,photo_id=map(unquote,rest.split('/photos/',1)); self._body(); return self._json(200,set_featured_photo(bid,photo_id))
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
        if path.endswith('/api/detection') or path=='/api/detection': return self._json(200,clear_detection_override())
        if '/api/bakes/' in path and '/photos/' in path:
            rest=path.split('/api/bakes/',1)[1]; bid,photo_id=map(unquote,rest.split('/photos/',1))
            try:delete_bake_photo(bid,photo_id); return self._json(200,{'deleted':photo_id})
            except (KeyError,ValueError):return self._json(404,{'error':'not_found'})
        return self.send_error(404)
    def log_message(self,fmt,*args): LOG.debug('Web UI: '+fmt,*args)

def start_web_ui():
    s=ThreadingHTTPServer(('0.0.0.0',8099),Handler); threading.Thread(target=s.serve_forever,daemon=True).start(); LOG.info('Webbgränssnitt startat: port=8099'); return s
def on_connect(client,userdata,flags,reason_code,properties=None):
    if getattr(reason_code,'is_failure',False): LOG.error('MQTT-anslutning misslyckades: %s',reason_code); return
    client.subscribe(f'{BASE_TOPIC}/cmd/#'); publish_discovery(); publish(f'{BASE_TOPIC}/state/active','ON' if session_active else 'OFF',True); LOG.info('MQTT ansluten och discovery publicerad')
def on_disconnect(client,userdata,disconnect_flags,reason_code,properties=None):
    if str(reason_code) not in {'0','Success','Normal disconnection'}: LOG.warning('MQTT frånkopplad: %s',reason_code)
    else: LOG.info('MQTT frånkopplad')
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
    c.will_set(f'{BASE_TOPIC}/availability','offline',retain=True); c.on_connect=on_connect; c.on_disconnect=on_disconnect; c.on_message=on_message; c.connect(host,port,60); c.loop_start(); mqtt_client=c
    LOG.info('MQTT-anslutning initierad: värd=%s port=%s tls=%s',host,port,ssl)

def main():
    global cfg,edge_history
    cfg=load_options(); log_level=configure_logging(cfg.get('log_level','info')); LOG.info('Startar Sourdough Monitor %s: loggnivå=%s kamerakälla=%s intervall=%ss',VERSION,log_level,cfg.get('camera_source','snapshot'),cfg.get('interval_seconds',60)); MEDIA_ROOT.mkdir(parents=True,exist_ok=True); init_db(); load_active_bake(); load_default_temperature_sensor(); cfg.update(DETECTION_DEFAULTS); cfg.update(load_roi_override()); cfg.update(load_detection_override()); edge_history=deque(maxlen=int(cfg['smoothing_frames'])); restore_session(); start_web_ui(); setup_mqtt(); publish(f'{BASE_TOPIC}/availability','online',True); LOG.info('Sourdough Monitor redo')
    while True:
        try:
            if session_active: process_frame(); time.sleep(max(1,int(cfg['interval_seconds'])))
            else: time.sleep(1)
        except CameraError as e: LOG.error('Kamerafel: %s',e); publish(f'{BASE_TOPIC}/state/status','camera_error',True); time.sleep(10)
        except requests.RequestException as e: LOG.error('Kamerafel: %s',safe_camera_error(e)); publish(f'{BASE_TOPIC}/state/status','camera_error',True); time.sleep(10)
        except Exception: LOG.exception('Fel i övervakningsloopen'); publish(f'{BASE_TOPIC}/state/status','error',True); time.sleep(10)
if __name__=='__main__': main()
