#!/usr/bin/env python3
"""Stdlib-only JSON stdin/stdout interchange bridge for SVS Studio projects."""
from __future__ import annotations

import json
import math
import struct
import sys
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

DEFAULT_PPQ=480
def uid(): return str(uuid.uuid4())
def err(message): raise ValueError(message)
def _project(p):
    if not isinstance(p,dict): err("project must be a JSON object")
    p.setdefault("schema",1); p.setdefault("name","Imported project"); p.setdefault("ppq",DEFAULT_PPQ); p.setdefault("sample_rate",24000)
    p.setdefault("tempos",[{"tick":0,"bpm":120.0}]); p.setdefault("time_signatures",[{"tick":0,"numerator":4,"denominator":4}]); p.setdefault("tracks",[]); p.setdefault("audio_clips",[])
    p.setdefault("mixer",{})
    if not isinstance(p["ppq"],int) or p["ppq"]<=0: err("project.ppq must be a positive integer")
    return p
def _path(req):
    if not req.get("path"): err("path is required")
    return Path(req["path"])
def _read(req): return req["content"] if "content" in req else _path(req).read_text(encoding="utf-8-sig")
def _write(req,text):
    if not req.get("path"): return {"content":text}
    p=_path(req); p.parent.mkdir(parents=True,exist_ok=True); p.write_text(text,encoding="utf-8",newline="\n"); return {"path":str(p)}
def _track(name,notes,kind="sing"):
    return {"id":uid(),"kind":kind,"name":name or "Imported","singer_id":"builtin.fallback" if kind=="sing" else "","voice":{},"instrument":"","volume":1.0,"pan":0.0,"mute":False,"solo":False,"colour":"","notes":notes,"curves":{},"fx":[],"sends":[],"bus":""}
def _note(tick,dur,key,lyric="",velocity=.8,legato=False):
    if tick<0 or dur<=0 or not 0<=key<=127: err("invalid note tick/duration/MIDI key")
    return {"id":uid(),"tick":int(tick),"duration":int(dur),"key":int(key),"velocity":float(velocity),"lyric":lyric,"phonemes":[],"legato":bool(legato),"head_ticks":0,"tail_ticks":0,"kind":"sung","vibrato":None,"attack_ms":None,"decay_ms":None}
def import_musicxml(req):
    root=ET.fromstring(_read(req)); ppq=int(req.get("project",{}).get("ppq",DEFAULT_PPQ)); tempos=[]; meters=[]; tracks=[]
    if root.tag.split('}')[-1] not in ("score-partwise","score-timewise"): err("unsupported MusicXML root")
    if root.tag.endswith("score-timewise"): err("score-timewise MusicXML is unsupported; convert to score-partwise")
    names={e.get("id"):(e.findtext("part-name") or e.get("id")) for e in root.findall("./{*}part-list/{*}score-part")}
    for part in root.findall("./{*}part"):
        notes=[]; cursor=0; divisions=1; ties={}; name=names.get(part.get("id"),part.get("id","Part"))
        for measure in part.findall("./{*}measure"):
            measure_start=cursor; local=0
            for el in measure:
                tag=el.tag.split('}')[-1]
                if tag=="attributes":
                    d=el.findtext("{*}divisions")
                    if d: divisions=int(d)
                    ts=el.find("{*}time")
                    if ts is not None: meters.append({"tick":measure_start,"numerator":int(ts.findtext("{*}beats","4")),"denominator":int(ts.findtext("{*}beat-type","4"))})
                elif tag=="direction":
                    sound=el.find("{*}sound")
                    if sound is not None and sound.get("tempo"):
                        offset=int(el.findtext("{*}offset","0")); tempos.append({"tick":measure_start+round((local+offset)*ppq/divisions),"bpm":float(sound.get("tempo"))})
                elif tag=="backup": local-=round(int(el.findtext("{*}duration","0"))*ppq/divisions)
                elif tag=="forward": local+=round(int(el.findtext("{*}duration","0"))*ppq/divisions)
                elif tag=="note":
                    dur=round(int(el.findtext("{*}duration","0"))*ppq/divisions); chord=el.find("{*}chord") is not None; start=measure_start+local
                    if el.find("{*}rest") is None:
                        pitch=el.find("{*}pitch")
                        if pitch is None: err("MusicXML unpitched notes are unsupported")
                        step=pitch.findtext("{*}step"); alter=int(pitch.findtext("{*}alter","0")); octave=int(pitch.findtext("{*}octave")); key=(octave+1)*12+{"C":0,"D":2,"E":4,"F":5,"G":7,"A":9,"B":11}[step]+alter
                        lyric=el.findtext("{*}lyric/{*}text",""); types=[x.get("type") for x in el.findall("{*}tie")]+[x.get("type") for x in el.findall("{*}notations/{*}tied")]; k=(key,lyric)
                        if "stop" in types and k in ties:
                            note=ties[k]; note["duration"]+=dur
                            if "start" not in types: ties.pop(k,None)
                        else:
                            note=_note(start,dur,key,lyric); notes.append(note)
                            if "start" in types: ties[k]=note
                    if not chord: local+=dur
            cursor=max(cursor,measure_start+local)
        if notes: tracks.append(_track(name,sorted(notes,key=lambda n:(n["tick"],n["key"]))))
    p=_project(req.get("project") or {}); p["tracks"]=tracks; p["tempos"]=sorted(tempos,key=lambda x:x["tick"]) or [{"tick":0,"bpm":120.0}]; p["time_signatures"]=sorted(meters,key=lambda x:x["tick"]) or [{"tick":0,"numerator":4,"denominator":4}]
    p["tempos"]=[{"tick":int(t["tick"]),"bpm":float(t["bpm"]),"curve":"step"} for t in p["tempos"]];p["tempos"].sort(key=lambda x:x["tick"])
    if p["tempos"][0]["tick"]!=0:p["tempos"].insert(0,{"tick":0,"bpm":p["tempos"][0]["bpm"],"curve":"step"})
    p["time_signatures"].sort(key=lambda x:x["tick"])
    return {"project":p}
def export_musicxml(req):
    p=_project(req.get("project")); parts=[t for t in p["tracks"] if t.get("notes")]
    if not parts: err("project has no note tracks")
    warnings=[]
    for t in parts:
        if t.get("curves") or t.get("volume",1)!=1 or t.get("pan",0)!=0 or t.get("mute") or t.get("solo"): warnings.append(f"track {t.get('name','')} mixer/curve state is not represented in MusicXML")
    if any(n.get("phonemes") or n.get("head_ticks",0) or n.get("tail_ticks",0) for t in parts for n in t["notes"]): err("MusicXML cannot preserve phonemes or consonant head/tail offsets")
    div=p["ppq"]; root=ET.Element("score-partwise",version="3.1"); pl=ET.SubElement(root,"part-list")
    for i,t in enumerate(parts,1): sp=ET.SubElement(pl,"score-part",id=f"P{i}"); ET.SubElement(sp,"part-name").text=t.get("name","Track")
    for i,t in enumerate(parts,1):
        part=ET.SubElement(root,"part",id=f"P{i}"); events={}
        for n in t["notes"]: events.setdefault(n["tick"],[]).append(n)
        end=max((n["tick"]+n["duration"] for n in t["notes"]),default=0); meters=sorted(p["time_signatures"],key=lambda x:x["tick"]); meter=meters[0] if meters else {"numerator":4,"denominator":4}
        if len(meters)>1 and any((x["numerator"],x["denominator"])!=(meter["numerator"],meter["denominator"]) for x in meters[1:]): err("MusicXML export currently requires a constant time signature")
        measure_ticks=round(div*meter["numerator"]*4/meter["denominator"]); measures=max(1,math.ceil((end+1)/measure_ticks))
        for m in range(measures):
            ms=m*measure_ticks; me=ms+measure_ticks; measure=ET.SubElement(part,"measure",number=str(m+1))
            if m==0:
                a=ET.SubElement(measure,"attributes"); ET.SubElement(a,"divisions").text=str(div); tm=ET.SubElement(a,"time"); ET.SubElement(tm,"beats").text=str(meter["numerator"]); ET.SubElement(tm,"beat-type").text=str(meter["denominator"])
            for tp in (x for x in p["tempos"] if ms<=x["tick"]<me):
                d=ET.SubElement(measure,"direction"); typ=ET.SubElement(d,"direction-type"); met=ET.SubElement(typ,"metronome"); ET.SubElement(met,"beat-unit").text="quarter"; ET.SubElement(met,"per-minute").text=str(tp["bpm"]); ET.SubElement(d,"sound",tempo=str(tp["bpm"]))
            cursor=ms
            for tick in sorted(k for k in events if ms<=k<me):
                if tick>cursor: _xml_rest(measure,tick-cursor); cursor=tick
                for j,n in enumerate(events[tick]):
                    x=ET.SubElement(measure,"note"); pitch=ET.SubElement(x,"pitch"); key=n["key"]; table=[("C",0),("C",1),("D",0),("D",1),("E",0),("F",0),("F",1),("G",0),("G",1),("A",0),("A",1),("B",0)]; step,alt=table[key%12]; ET.SubElement(pitch,"step").text=step
                    if alt: ET.SubElement(pitch,"alter").text=str(alt)
                    ET.SubElement(pitch,"octave").text=str(key//12-1); ET.SubElement(x,"duration").text=str(n["duration"])
                    if j: ET.SubElement(x,"chord")
                    if n.get("lyric"): ly=ET.SubElement(x,"lyric"); ET.SubElement(ly,"text").text=n["lyric"]
                cursor=max(cursor,max(n["tick"]+n["duration"] for n in events[tick]))
            if cursor<me: _xml_rest(measure,me-cursor)
    return {**_write(req,ET.tostring(root,encoding="unicode",xml_declaration=True)),"warnings":warnings}
def _xml_rest(parent,dur):
    n=ET.SubElement(parent,"note"); ET.SubElement(n,"rest"); ET.SubElement(n,"duration").text=str(dur)
def _vlq(n):
    n=max(0,int(n)); out=[n&127]; n>>=7
    while n: out.insert(0,(n&127)|128); n>>=7
    return bytes(out)
def _read_vlq(data,i):
    v=0
    while True:
        b=data[i]; i+=1; v=(v<<7)|(b&127)
        if b<128:return v,i
def _midi_parse(data):
    if data[:4]!=b"MThd" or len(data)<14: err("invalid MIDI header")
    fmt,ntracks,division=struct.unpack(">HHH",data[8:14])
    if fmt==2: err("asynchronous format-2 MIDI is unsupported")
    if division&0x8000: err("SMPTE-time-division MIDI is unsupported")
    pos=14; tracks=[]
    for _ in range(ntracks):
        if data[pos:pos+4]!=b"MTrk": err("invalid MIDI track chunk")
        length=struct.unpack(">I",data[pos+4:pos+8])[0]; d=data[pos+8:pos+8+length]; pos+=8+length; i=0; tick=0; running=None; active={}; events=[]
        while i<len(d):
            delta=0
            while True:
                b=d[i]; i+=1; delta=(delta<<7)|(b&127)
                if b<128:break
            tick+=delta; status=d[i]
            if status<128:
                if running is None:err("invalid MIDI running status")
                status=running
            else:i+=1
            if status==255:
                typ=d[i]; i+=1; size,i=_read_vlq(d,i); payload=d[i:i+size]; i+=size
                if typ==81 and size==3:events.append(("tempo",tick,60_000_000/int.from_bytes(payload,"big")))
                elif typ==88 and size>=2:events.append(("meter",tick,payload[0],2**payload[1]))
                elif typ==5:events.append(("lyric",tick,payload.decode("utf-8")))
                elif typ==47:break
                running=None;continue
            if status in (240,247):size,i=_read_vlq(d,i);i+=size;running=None;continue
            running=status;cmd=status&240;chan=status&15;size=1 if cmd in (192,208) else 2;a=d[i];i+=1;b=d[i] if size==2 else 0
            if size==2:i+=1
            if cmd==144 and b:active.setdefault((chan,a),[]).append((tick,b))
            elif cmd==128 or (cmd==144 and b==0):
                starts=active.get((chan,a),[])
                if starts:
                    start=starts.pop(0)
                    if not starts:active.pop((chan,a),None)
                    events.append(("note",start[0],tick-start[0],a,start[1],chan))
        tracks.append(events)
    return fmt,division,tracks
def import_midi(req):
    raw=Path(req["path"]).read_bytes() if "content" not in req else bytes.fromhex(req["content"]); fmt,division,rawtracks=_midi_parse(raw); p=_project(req.get("project") or {}); ppq=p["ppq"]; tempos=[];meters=[];out=[];lyrics={}
    for events in rawtracks:
        for e in events:
            if e[0]=="lyric":lyrics.setdefault(round(e[1]*ppq/division),[]).append(e[2])
    for i,events in enumerate(rawtracks):
        notes=[]
        for e in events:
            if e[0]=="tempo":tempos.append({"tick":round(e[1]*ppq/division),"bpm":e[2]})
            elif e[0]=="meter":meters.append({"tick":round(e[1]*ppq/division),"numerator":e[2],"denominator":e[3]})
            elif e[0]=="note":
                tick=round(e[1]*ppq/division); lyric=lyrics.get(tick,[]); notes.append(_note(tick,max(1,round(e[2]*ppq/division)),e[3],lyric.pop(0) if lyric else "",e[4]/127))
        if notes:out.append(_track(f"MIDI Track {i+1}",notes,"instrument"))
    p["tracks"]=out;p["tempos"]=[{"tick":int(t["tick"]),"bpm":float(t["bpm"]),"curve":"step"} for t in (sorted(tempos,key=lambda x:x["tick"]) or p["tempos"])];p["time_signatures"]=sorted(meters,key=lambda x:x["tick"]) or p["time_signatures"]
    if p["tempos"][0]["tick"]!=0:p["tempos"].insert(0,{"tick":0,"bpm":p["tempos"][0]["bpm"],"curve":"step"})
    return {"project":p,"format":fmt}
def export_midi(req):
    p=_project(req.get("project"));ppq=p["ppq"];division=int(req.get("division",ppq));tracks=[t for t in p["tracks"] if t.get("notes")];warnings=[]
    if not 1<=division<=32767:err("MIDI division must be 1..32767")
    if not tracks:err("project has no note tracks")
    if any(t.get("curves") or t.get("volume",1)!=1 or t.get("pan",0)!=0 or t.get("mute") or t.get("solo") for t in tracks):warnings.append("MIDI does not represent track curves, volume, pan, mute, or solo; notes exported regardless")
    if any(n.get("phonemes") or n.get("head_ticks",0) or n.get("tail_ticks",0) for t in tracks for n in t["notes"]):err("MIDI cannot preserve phonemes or consonant head/tail offsets")
    multi=bool(req.get("multitrack",False)); chunks=[];meta=[]
    for x in p["tempos"]:meta.append((round(x["tick"]*division/ppq),b"\xff\x51\x03"+round(60_000_000/x["bpm"]).to_bytes(3,"big")))
    for x in p["time_signatures"]:meta.append((round(x["tick"]*division/ppq),bytes((255,88,4,x["numerator"],round(math.log2(x["denominator"])),24,8))))
    chunks.append(_track_chunk(meta))
    if multi: groups=[[t] for t in tracks]
    else:
        lead=next((t for t in tracks if t.get("kind")=="sing"),tracks[0]); accompaniment=[t for t in tracks if t is not lead]
        groups=[[lead],accompaniment] if accompaniment else [[lead]]
        if len(accompaniment)>1:warnings.append(f"combined {len(accompaniment)} accompaniment tracks into the second MIDI track")
    for ch,group in enumerate(groups):
        ev=[]
        for t in group:
            channel=9 if t.get("instrument","").lower() in ("drum","drums","percussion") else ch%16
            for n in t["notes"]:
                start=round(n["tick"]*division/ppq);end=round((n["tick"]+n["duration"])*division/ppq);key=n["key"];vel=max(1,min(127,round(n.get("velocity",.8)*127)))
                if n.get("lyric"):
                    lyric=n["lyric"].encode("utf-8");ev.append((start,b"\xff\x05"+_vlq(len(lyric))+lyric))
                ev.extend([(start,bytes((0x90+channel,key,vel))),(end,bytes((0x80+channel,key,0)))])
        chunks.append(_track_chunk(ev))
    body=b"MThd"+struct.pack(">IHHH",6,1,len(chunks),division)+b"".join(chunks)
    if req.get("path"):
        path=_path(req);path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(body);return {"path":str(path),"warnings":warnings}
    return {"content":body.hex(),"warnings":warnings}
def _track_chunk(events):
    events=sorted(events,key=lambda e:(e[0],0 if e[1][0]&240==128 else 1));body=bytearray();last=0
    for tick,msg in events:body+=_vlq(tick-last)+msg;last=tick
    body+=b"\x00\xff\x2f\x00";return b"MTrk"+struct.pack(">I",len(body))+body
def _tick_ms(tempos,tick,ppq):
    ms=0.;pts=sorted(tempos or [{"tick":0,"bpm":120}],key=lambda x:x["tick"])
    for i,p in enumerate(pts):
        end=pts[i+1]["tick"] if i+1<len(pts) else tick
        if tick<=end:return ms+max(0,tick-p["tick"])*60000/(p["bpm"]*ppq)
        ms+=(end-p["tick"])*60000/(p["bpm"]*ppq)
    return ms
def export_lrc(req):
    p=_project(req.get("project"));lines=[]
    for t in p["tracks"]:
        for n in sorted(t.get("notes",[]),key=lambda x:x["tick"]):
            if n.get("lyric"):
                ms=_tick_ms(p["tempos"],n["tick"],p["ppq"]);lines.append((ms,f"[{int(ms//60000):02d}:{(ms%60000)/1000:05.2f}]{n['lyric']}"))
    return _write(req,"\n".join(x[1] for x in sorted(lines))+("\n" if lines else ""))
def import_ufdata(req):
    data=json.loads(_read(req));p=_project(req.get("project") or {})
    if not isinstance(data,dict):err("ufdata must be a JSON object")
    rawtracks=data.get("tracks") or data.get("voices") or [{"name":data.get("name","UFData"),"notes":data.get("notes",[])}];tracks=[]
    for i,t in enumerate(rawtracks):
        notes=[]
        for n in t.get("notes",[]):notes.append(_note(int(n.get("tick",n.get("position",0))),int(n.get("duration",n.get("length",0))),int(n.get("key",n.get("noteNum",60))),n.get("lyric",n.get("lyrics","")),float(n.get("velocity",.8)),bool(n.get("legato",False))))
        if notes:tracks.append(_track(t.get("name",f"UFData {i+1}"),notes,"sing"))
    p["tracks"]=tracks
    if data.get("tempos"):p["tempos"]=[{"tick":int(x.get("tick",x.get("position",0))),"bpm":float(x["bpm"])} for x in data["tempos"]]
    elif data.get("bpm"):p["tempos"]=[{"tick":0,"bpm":float(data["bpm"])}]
    if data.get("time_signatures"):p["time_signatures"]=[{"tick":int(x.get("tick",0)),"numerator":int(x["numerator"]),"denominator":int(x["denominator"])} for x in data["time_signatures"]]
    p["tempos"]=[{"tick":int(t["tick"]),"bpm":float(t["bpm"]),"curve":"step"} for t in p["tempos"]];p["tempos"].sort(key=lambda x:x["tick"])
    if not p["tempos"] or p["tempos"][0]["tick"]!=0:p["tempos"].insert(0,{"tick":0,"bpm":p["tempos"][0]["bpm"] if p["tempos"] else 120.0,"curve":"step"})
    p["time_signatures"].sort(key=lambda x:x["tick"])
    return {"project":p}
import importlib.util
_bundle_path=Path(__file__).with_name("project_bundle.py")
_bundle_spec=importlib.util.spec_from_file_location("svs_score_project_bundle",_bundle_path)
if _bundle_spec is None or _bundle_spec.loader is None: raise RuntimeError(f"Cannot load project bundle module at {_bundle_path}")
_bundle_module=importlib.util.module_from_spec(_bundle_spec)
_bundle_spec.loader.exec_module(_bundle_module)
export_bundle=_bundle_module.export_bundle
import_bundle=_bundle_module.import_bundle
ACTIONS={"import_musicxml":import_musicxml,"export_musicxml":export_musicxml,"import_midi":import_midi,"export_midi":export_midi,"export_lrc":export_lrc,"import_ufdata":import_ufdata,"export_bundle":export_bundle,"import_bundle":import_bundle}
def dispatch(req):
    if not isinstance(req,dict):err("request must be a JSON object")
    action=req.get("action")
    if action not in ACTIONS:err("action must be one of: "+", ".join(ACTIONS))
    return ACTIONS[action](req)
def main():
    try:json.dump({"ok":True,**dispatch(json.load(sys.stdin))},sys.stdout,ensure_ascii=False)
    except Exception as e:json.dump({"ok":False,"error":str(e)},sys.stdout,ensure_ascii=False);return 1
    return 0
if __name__=="__main__":raise SystemExit(main())
