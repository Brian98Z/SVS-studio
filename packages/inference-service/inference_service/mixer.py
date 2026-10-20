"""Deterministic offline stereo mixer with serializable track and bus routing."""
from __future__ import annotations
import base64, math
from typing import Any, Mapping, Sequence
import numpy as np

def default_mixer_config(): return {"master":{"volume":1.0,"effects":[]},"buses":[]}
def _finite(v,label,lo,hi):
    try:x=float(v)
    except (TypeError,ValueError):raise ValueError(f"{label} must be finite") from None
    if not math.isfinite(x) or not lo<=x<=hi:raise ValueError(f"{label} must be between {lo} and {hi}")
    return x
def _effects(es):
    if not isinstance(es,list):raise ValueError("effects must be an array")
    out=[]
    for e in es:
        if not isinstance(e,Mapping):raise ValueError("invalid effect type")
        k=e.get("type")
        if k not in ("eq3","reverb","compressor","vst3"):raise ValueError("invalid effect type")
        x={"type":k,"enabled":bool(e.get("enabled",False))}
        if k=="vst3":
            from .vst_host import resolve_plugin_path, VSTHostError
            path=e.get("path")
            try: resolve_plugin_path(path)
            except VSTHostError as exc: raise ValueError(str(exc)) from exc
            params=e.get("parameters",{})
            if not isinstance(params,Mapping):raise ValueError("VST3 parameters must be an object")
            clean={}
            for name,value in params.items():
                if not isinstance(name,str) or not name or not isinstance(value,(bool,int,float,str)) or isinstance(value,float) and not math.isfinite(value):raise ValueError("VST3 parameters must have names and finite numeric, boolean, or string values")
                clean[name]=value
            raw_block=e.get("block_frames",4096)
            if isinstance(raw_block,bool) or not isinstance(raw_block,int) or not 1<=raw_block<=65536:raise ValueError("VST3 block_frames must be an integer from 1 to 65536")
            from .vst_host import validate_state
            try: state=validate_state(e.get("state_base64"))
            except VSTHostError as exc: raise ValueError(str(exc)) from exc
            x.update(path=path,parameters=clean,block_frames=raw_block,tail_seconds=_finite(e.get("tail_seconds",0),"VST3 tail seconds",0,10),state_base64=state)
        elif k=="eq3":x.update(low_db=_finite(e.get("low_db",0),"EQ low",-24,24),mid_db=_finite(e.get("mid_db",0),"EQ mid",-24,24),high_db=_finite(e.get("high_db",0),"EQ high",-24,24),low_hz=_finite(e.get("low_hz",250),"EQ low Hz",40,2000),high_hz=_finite(e.get("high_hz",4000),"EQ high Hz",1000,18000))
        elif k=="reverb":x.update(wet=_finite(e.get("wet",.15),"reverb wet",0,1),room=_finite(e.get("room",.5),"reverb room",0,1))
        else:x.update(threshold_db=_finite(e.get("threshold_db",-18),"threshold",-60,0),ratio=_finite(e.get("ratio",4),"ratio",1,20),attack_ms=_finite(e.get("attack_ms",10),"attack",.1,200),release_ms=_finite(e.get("release_ms",100),"release",1,2000))
        out.append(x)
    return out
def validate_config(c):
    if not isinstance(c,Mapping):raise ValueError("mixer config must be an object")
    master=c.get("master",{});buses=c.get("buses",[])
    if not isinstance(master,Mapping) or not isinstance(buses,list):raise ValueError("invalid mixer config")
    ids=set();bs=[]
    for b in buses:
        if not isinstance(b,Mapping):raise ValueError("bus must be an object")
        i=b.get("id")
        if not isinstance(i,str) or not i or i=="master" or i in ids:raise ValueError("bus ids must be unique non-empty strings other than master")
        ids.add(i);out=b.get("output","master");sends=b.get("sends",[])
        if not isinstance(out,str) or not isinstance(sends,list):raise ValueError("invalid bus routing")
        ss=[]
        for s in sends:
            if not isinstance(s,Mapping) or not isinstance(s.get("to"),str):raise ValueError("invalid send")
            ss.append({"to":s["to"],"level":_finite(s.get("level",1),"send level",0,4)})
        bs.append({"id":i,"name":str(b.get("name",i)),"output":out,"volume":_finite(b.get("volume",1),"bus volume",0,4),"pan":_finite(b.get("pan",0),"bus pan",-1,1),"effects":_effects(b.get("effects",[])),"sends":ss})
    graph={i:set() for i in ids|{"master"}}
    for b in bs:
        if b["output"] not in graph:raise ValueError("unknown bus output")
        if b["output"]!="master":graph[b["id"]].add(b["output"])
        for s in b["sends"]:
            if s["to"] not in graph or s["to"]==b["id"]:raise ValueError("invalid send destination")
            graph[b["id"]].add(s["to"])
    active=set();done=set()
    def visit(n):
        if n in active:raise ValueError("bus routing contains a cycle")
        if n in done:return
        active.add(n)
        for d in graph[n]:visit(d)
        active.remove(n);done.add(n)
    for n in graph:visit(n)
    return {"master":{"volume":_finite(master.get("volume",1),"master volume",0,4),"effects":_effects(master.get("effects",[]))},"buses":bs}
def pan_stereo(signal,pan=0):
    p=_finite(pan,"pan",-1,1);a=np.asarray(signal,dtype=np.float32)
    if a.ndim==1:
        theta=(p+1)*math.pi/4
        gl=0.0 if p==1 else (1.0 if p==-1 else math.cos(theta))
        gr=0.0 if p==-1 else (1.0 if p==1 else math.sin(theta))
        return np.column_stack((a*gl,a*gr)).astype(np.float32,copy=False)
    if a.ndim==2 and a.shape[1]==2:
        gl=0 if p==1 else (1 if p<=0 else math.cos(p*math.pi/2));gr=0 if p==-1 else (math.cos(-p*math.pi/2) if p<0 else 1)
        return (a*np.array([gl,gr],np.float32)).astype(np.float32,copy=False)
    raise ValueError("audio must be mono frames or (frames, 2)")


def _filter(x,rate,hz,low):
    w=2*math.pi*min(hz,rate*.45)/rate;c=math.cos(w);s=math.sin(w);a=s/math.sqrt(2);d=1+a;b0=((1-c)/2 if low else (1+c)/2)/d;b1=(1-c if low else -(1+c))/d;b2=b0;e=-2*c/d;f=(1-a)/d;y=np.empty_like(x);x1=x2=y1=y2=0.
    for i,v in enumerate(x):z=b0*float(v)+b1*x1+b2*x2-e*y1-f*y2;y[i]=z;x2=x1;x1=float(v);y2=y1;y1=z
    return y

def process_effects(audio,effects,sample_rate):
    data=np.asarray(audio,dtype=np.float32).copy()
    for e in _effects(list(effects)):
        if not e["enabled"]:continue
        if e["type"]=="eq3":
            for ch in range(2):
                x=data[:,ch];lo=_filter(x,sample_rate,e["low_hz"],True);below=_filter(x,sample_rate,e["high_hz"],True);hi=x-below;mid=below-lo
                data[:,ch]=lo*10**(e["low_db"]/20)+mid*10**(e["mid_db"]/20)+hi*10**(e["high_db"]/20)
        elif e["type"]=="reverb":
            dry=data.copy();wet=np.zeros_like(data)
            for ch in range(2):
                delay=max(1,int(sample_rate*(.035+.11*e["room"])*(1+.08*ch)));decay=.25+.65*e["room"]
                for n in range(delay,len(data)):wet[n,ch]=dry[n-delay,ch]+wet[n-delay,ch]*decay
            data=dry*(1-e["wet"])+wet*e["wet"]*.35
        elif e["type"]=="compressor":
            threshold=10**(e["threshold_db"]/20);attack=math.exp(-1/(sample_rate*e["attack_ms"]*.001));release=math.exp(-1/(sample_rate*e["release_ms"]*.001));env=0.
            for n in range(len(data)):
                peak=float(np.max(np.abs(data[n])));coef=attack if peak>env else release;env=coef*env+(1-coef)*peak
                if env>threshold:data[n]*=threshold*(env/threshold)**(1/e["ratio"])/env
        elif e["type"]=="vst3":
            from inference_service.vst_host import process_effect
            rendered=process_effect(e["path"],data.T,sample_rate,parameters=e["parameters"],state_base64=e["state_base64"],block_frames=e["block_frames"],tail_seconds=e["tail_seconds"])
            data=np.asarray(rendered,dtype=np.float32).T
    return data

def mix_tracks(tracks,sample_rate,config=None):
    c=validate_config(config or default_mixer_config());bs={b["id"]:b for b in c["buses"]};acc={}
    def add(dest,x):
        if dest not in acc:acc[dest]=np.zeros_like(x)
        if len(acc[dest])<len(x):acc[dest]=np.pad(acc[dest],((0,len(x)-len(acc[dest])),(0,0)))
        acc[dest][:len(x)]+=x
    for t in tracks:
        x=pan_stereo(t["audio"],t.get("pan",0))*_finite(t.get("volume",1),"track volume",0,4);x=process_effects(x,t.get("effects",[]),sample_rate);bus=t.get("bus") or "master"
        if bus!="master" and bus not in bs:raise ValueError(f"unknown track bus: {bus}")
        add(bus,x)
        for s in t.get("sends",[]):
            if s.get("to") not in bs and s.get("to")!="master":raise ValueError("unknown send bus")
            add(s["to"],x*_finite(s.get("level",1),"send level",0,4))
    pending=set(bs)
    while pending:
        ready=[i for i in pending if not any(i in ([bs[src]["output"]] if bs[src]["output"]!="master" else [])+[s["to"] for s in bs[src]["sends"] if s["to"]!="master"] for src in pending)]
        if not ready:raise ValueError("bus routing contains a cycle")
        for i in ready:
            b=bs[i];x=acc.get(i,np.zeros((0,2),np.float32))*b["volume"]
            x=pan_stereo(x,b.get("pan",0));processed=process_effects(x,b["effects"],sample_rate)
            for s in b["sends"]:add(s["to"],processed*s["level"])
            add(b["output"],processed);pending.remove(i)
    return process_effects(acc.get("master",np.zeros((0,2),np.float32))*c["master"]["volume"],c["master"]["effects"],sample_rate)

def register_routes(app: Any) -> None:
    from flask import jsonify,request
    @app.post("/mix/validate")
    def mix_validate():
        try:return jsonify({"ok":True,"mixer":validate_config(request.get_json(force=True,silent=True) or {})})
        except (ValueError,TypeError,OverflowError) as e:return jsonify({"error":"BAD_REQUEST","detail":str(e)}),400
    @app.post("/mix")
    def mix_route():
        body=request.get_json(force=True,silent=True) or {}
        try:
            rate=int(body.get("sample_rate",24000));tracks=body.get("tracks",[])
            if not 8000<=rate<=192000 or not isinstance(tracks,list):raise ValueError("invalid sample rate or tracks")
            solo=any(bool(t.get("solo")) for t in tracks if isinstance(t,Mapping));items=[]
            for t in tracks:
                if not isinstance(t,Mapping) or not isinstance(t.get("audio"),str):raise ValueError("each track requires base64 audio")
                if t.get("mute") or solo and not t.get("solo"):continue
                pcm=np.frombuffer(base64.b64decode(t["audio"],validate=True),dtype="<f4").astype(np.float32,copy=False);ch=int(t.get("channels",1))
                if ch not in (1,2) or len(pcm)%ch or not np.all(np.isfinite(pcm)):raise ValueError("invalid track PCM")
                audio=pcm if ch==1 else pcm.reshape((-1,2));vol=_finite(t.get("volume",1),"track volume",0,4)*_finite(t.get("bus_gain",1),"bus gain",0,4)
                items.append({"audio":audio,"volume":vol,"pan":t.get("pan",0),"bus":t.get("bus") or "master","effects":t.get("fx",t.get("effects",[])),"sends":t.get("sends",[])})
            cfg={"master":{"volume":body.get("master_volume",1),"effects":body.get("master_fx",[])},"buses":body.get("buses",[])};out=mix_tracks(items,rate,cfg)
            return jsonify({"audio":base64.b64encode(out.astype("<f4",copy=False).tobytes()).decode("ascii"),"sample_rate":rate,"channels":2,"frames":len(out)})
        except (ValueError,TypeError,OverflowError) as e:return jsonify({"error":"BAD_REQUEST","detail":str(e)}),400
        except Exception as e:
            from inference_service.vst_host import VSTHostError
            if not isinstance(e,VSTHostError):raise
            return jsonify({"error":"VST_PROCESSING_ERROR","detail":str(e)}),422
