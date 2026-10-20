"""Local media probe/import and interchange helpers for SVS Studio.

HTTP handlers are opt-in through register_routes(app), keeping service_runner's
lifecycle and route namespace under its owner's control.
"""
from __future__ import annotations

import json
import math
from .contract import tick_to_ms as contract_tick_to_ms
import os
import shutil
import subprocess
import tempfile
import uuid
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping, Sequence

from flask import jsonify, request

AUDIO_EXTS = {".wav", ".wave", ".mp3", ".flac", ".ogg", ".oga", ".m4a", ".aac", ".opus", ".aiff", ".aif"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpeg", ".mpg"}


class MediaError(ValueError): pass


def _tool(name: str) -> str:
    path = shutil.which(name)
    if not path: raise MediaError(f"Required executable not found on PATH: {name}")
    return path


def _path(value: Any, formats: set[str]) -> Path:
    if not isinstance(value, str) or not value.strip(): raise MediaError("path is required")
    path = Path(value).expanduser().resolve(strict=True)
    if not path.is_file(): raise MediaError("path must identify a regular local file")
    if path.suffix.lower() not in formats: raise MediaError(f"Unsupported media extension: {path.suffix or '(none)'}")
    return path


def _probe(path: Path) -> dict[str, Any]:
    result = subprocess.run([_tool("ffprobe"), "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)], capture_output=True, text=True, check=False)
    if result.returncode: raise MediaError(f"ffprobe rejected media: {result.stderr.strip() or 'unknown error'}")
    try: return json.loads(result.stdout)
    except json.JSONDecodeError as exc: raise MediaError("ffprobe returned invalid metadata") from exc


def probe_media(filename: str) -> dict[str, Any]:
    path = Path(filename).expanduser().resolve(strict=True)
    if path.suffix.lower() not in AUDIO_EXTS | VIDEO_EXTS: raise MediaError(f"Unsupported media extension: {path.suffix or '(none)'}")
    info = _probe(path); streams = info.get("streams", [])
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    video = next((s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")), None)
    if not audio and not video: raise MediaError("File has no supported audio or video stream")
    return {"path": str(path), "name": path.name, "duration_seconds": _duration(info), "audio": _stream(audio), "video": _stream(video), "format": info.get("format", {}).get("format_name")}


def _duration(data: Mapping[str, Any]) -> float | None:
    vals = [data.get("format", {}).get("duration")] + [s.get("duration") for s in data.get("streams", [])]
    for value in vals:
        try:
            v = float(value)
            if math.isfinite(v) and v >= 0: return v
        except (TypeError, ValueError): pass
    return None


def _stream(stream: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if stream is None: return None
    keys = ("codec_name", "sample_rate", "channels", "channel_layout", "width", "height", "avg_frame_rate", "pix_fmt")
    return {k: stream[k] for k in keys if stream.get(k) is not None}


def decode_audio(filename: str, sample_rate: int = 24000) -> dict[str, Any]:
    path = _path(filename, AUDIO_EXTS | VIDEO_EXTS)
    if not isinstance(sample_rate, int) or not 8000 <= sample_rate <= 192000: raise MediaError("sample_rate must be an integer from 8000 through 192000")
    proc = subprocess.run([_tool("ffmpeg"), "-v", "error", "-i", str(path), "-map", "0:a:0", "-vn", "-ac", "2", "-ar", str(sample_rate), "-f", "f32le", "pipe:1"], capture_output=True, check=False)
    if proc.returncode or not proc.stdout: raise MediaError(f"ffmpeg audio decode failed: {proc.stderr.decode(errors='replace').strip()}")
    if len(proc.stdout) % 8: raise MediaError("Decoder returned incomplete stereo float frames")
    return {"path": str(path), "sample_rate": sample_rate, "channels": 2, "frames": len(proc.stdout) // 8, "encoding": "f32le", "audio": proc.stdout}


def ticks_to_seconds(tick: float, tempos: Sequence[Mapping[str, Any]], ppq: int) -> float:
    if ppq <= 0 or tick < 0: raise MediaError("ppq must be positive and tick nonnegative")
    if not tempos: return tick * 60.0 / (ppq * 120.0)
    try:
        normalized=sorted(tempos,key=lambda p:int(p["tick"]))
        if normalized[0]["tick"]!=0: raise MediaError("tempo table must start at tick 0")
        if any(not math.isfinite(float(p["bpm"])) or float(p["bpm"])<=0 for p in normalized): raise MediaError("tempo BPM must be finite and positive")
        if any(int(b["tick"])<=int(a["tick"]) for a,b in zip(normalized,normalized[1:])): raise MediaError("tempo ticks must be strictly increasing")
        if ppq == 480: return contract_tick_to_ms(normalized,tick)/1000.0
        scaled=[{**point,"tick":int(round(int(point["tick"])*480/ppq))} for point in normalized]
        return contract_tick_to_ms(scaled,int(round(tick*480/ppq)))/1000.0
    except (KeyError,ValueError,TypeError,ZeroDivisionError) as exc:
        if isinstance(exc,MediaError): raise
        raise MediaError(f"Invalid tempo map: {exc}") from exc


def _export_clips(project: Mapping[str, Any], stems: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    if stems:
        return [dict(stem, start_tick=0, track_id=stem.get("track_id", stem.get("track_name", "stem"))) for stem in stems if stem.get("path")]
    return [dict(clip) for clip in project.get("audio_clips", [])]


def export_fcpxml(project: Mapping[str, Any], output_path: str, stems: Sequence[Mapping[str, Any]] | None = None) -> Path:
    dest = _new_output(output_path, {".fcpxml"}); ppq = int(project.get("ppq", 0)); tempos = project.get("tempos", [])
    clips = _export_clips(project,stems); root = ET.Element("fcpxml", version="1.10")
    if not clips: raise MediaError("FCPXML export needs imported clips or rendered stems")
    resources = ET.SubElement(root, "resources")
    ET.SubElement(resources, "format", id="r0", name="FFVideoFormat1080p30", frameDuration="1/30s", width="1920", height="1080", colorSpace="1-1-1 (Rec. 709)")
    event=ET.SubElement(ET.SubElement(root,"library"),"event",name=str(project.get("name","SVS Studio")))
    p=ET.SubElement(event,"project",name=str(project.get("name","SVS Studio")))
    seq=ET.SubElement(p,"sequence",format="r0",duration="0s",tcFormat="NDF",audioLayout="stereo",audioRate="48k")
    spine=ET.SubElement(seq,"spine"); primary=ET.SubElement(spine,"gap",name="Timeline",offset="0s",duration="0s"); resources_map={}; max_end=0.0; lane_by_track={}
    for i,clip in enumerate(clips):
        source=_path(clip.get("path"),AUDIO_EXTS | VIDEO_EXTS); meta=probe_media(str(source)); duration=meta["duration_seconds"]
        if not duration: raise MediaError(f"Cannot determine clip duration: {source.name}")
        start=ticks_to_seconds(int(clip.get("start_tick",0)),tempos,ppq); rid=f"a{i+1}"; track=clip.get("track_id",f"track-{i}")
        lane=lane_by_track.setdefault(track,len(lane_by_track)+1)
        prior=resources_map.get(track,[])
        if any(start < old_end and start+duration > old_start for old_start,old_end in prior): raise MediaError(f"Overlapping clips on track {track} cannot be represented safely")
        prior.append((start,start+duration)); resources_map[track]=prior
        ET.SubElement(resources,"asset",id=rid,name=source.name,src=source.as_uri(),start="0s",duration=_time(duration),hasVideo="0" if not meta["video"] else "1",hasAudio="1")
        ET.SubElement(primary,"asset-clip",name=source.name,ref=rid,lane=str(lane),offset=_time(start),duration=_time(duration),start="0s",audioRole="dialogue")
        max_end=max(max_end,start+duration)
    primary.set("duration",_time(max_end)); seq.set("duration",_time(max_end))
    _atomic_write(dest,ET.tostring(root,encoding="utf-8",xml_declaration=True)); return dest

def export_aaf(project: Mapping[str, Any], output_path: str, stems: Sequence[Mapping[str, Any]] | None = None) -> Path:
    """Write valid AAF with PCM essence and a composition containing timed source clips."""
    clips=_export_clips(project,stems)
    try: import aaf2
    except ImportError as exc: raise MediaError("AAF export requires pyaaf2; install from repository-root requirements-media.txt") from exc
    dest=_new_output(output_path,{".aaf"}); ppq=int(project.get("ppq",0)); tempos=project.get("tempos",[])
    clips=_export_clips(project,stems)
    if not clips: raise MediaError("AAF export needs at least one audio clip")
    # AAF PCM essence import supports WAV; decode all accepted audio into temporary PCM WAV.
    import wave
    try:
        with tempfile.TemporaryDirectory(prefix="svs-aaf-") as tmpdir:
            with aaf2.open(str(dest),"w") as f:
                comp=f.create.CompositionMob(str(project.get("name","SVS Studio"))); f.content.mobs.append(comp)
                grouped={}
                for i,clip in enumerate(clips): grouped.setdefault(clip.get("track_id","audio"),[]).append((i,clip))
                for track_index,(track_id,items) in enumerate(grouped.items()):
                    slot=comp.create_sound_slot(edit_rate=48000); sequence=slot.segment; cursor=0
                    for i,clip in sorted(items,key=lambda pair:int(pair[1].get("start_tick",0))):
                        src=_path(clip.get("path"),AUDIO_EXTS | VIDEO_EXTS); meta=probe_media(str(src))
                        if not meta["audio"]: raise MediaError(f"Clip has no audio stream: {src.name}")
                        start=int(round(ticks_to_seconds(int(clip.get("start_tick",0)),tempos,ppq)*48000))
                        if start<cursor: raise MediaError(f"Overlapping audio clips on track {track_id} are not supported")
                        if start>cursor: sequence.components.append(f.create.Filler("sound",start-cursor)); cursor=start
                        wav=Path(tmpdir)/f"clip-{i}.wav"
                        proc=subprocess.run([_tool("ffmpeg"),"-nostdin","-v","error","-y","-i",str(src),"-map","0:a:0","-vn","-ac","2","-ar","48000","-c:a","pcm_s16le",str(wav)],capture_output=True,text=True)
                        if proc.returncode: raise MediaError(f"PCM conversion failed for {src.name}: {proc.stderr.strip()}")
                        with wave.open(str(wav),"rb") as reader: frames=reader.getnframes()
                        mob=f.create.MasterMob(src.name); f.content.mobs.append(mob)
                        source_slot=mob.import_audio_essence(str(wav),48000)
                        sequence.components.append(mob.create_source_clip(source_slot.slot_id,length=frames,media_kind="sound")); cursor+=frames
                    sequence.length=cursor
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    return dest



def assemble_video(video_path: str, audio_path: str, output_path: str) -> Path:
    video=_path(video_path,VIDEO_EXTS); audio=_path(audio_path,AUDIO_EXTS)
    if not probe_media(str(video))["video"]: raise MediaError("video_path has no video stream")
    if not probe_media(str(audio))["audio"]: raise MediaError("audio_path has no audio stream")
    dest=_new_output(output_path,{".mp4",".mov",".mkv"}); suffix=["-c:v","copy"]
    _run_atomic_ffmpeg([_tool("ffmpeg"),"-nostdin","-v","error","-i",str(video),"-i",str(audio),"-map","0:v:0","-map","1:a:0",*suffix,"-shortest",str(dest)],dest); return dest




def _time(seconds: float) -> str:
    f=Fraction(seconds).limit_denominator(1000000); return f"{f.numerator}/{f.denominator}s"


def _new_output(value: str, extensions: set[str]) -> Path:
    if not isinstance(value,str) or not value.strip(): raise MediaError("output_path is required")
    path=Path(value).expanduser().resolve()
    if path.suffix.lower() not in extensions: raise MediaError(f"Output must use one of: {', '.join(sorted(extensions))}")
    if path.exists(): raise MediaError(f"Refusing to overwrite existing output: {path}")
    if not path.parent.is_dir(): raise MediaError("Output directory must already exist")
    return path


def _atomic_write(path: Path,data: bytes) -> None:
    tmp=path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("xb") as f: f.write(data); f.flush(); os.fsync(f.fileno())
        os.link(tmp,path)
    finally: tmp.unlink(missing_ok=True)


def _run_atomic_ffmpeg(command: list[str],destination: Path) -> None:
    temp=destination.with_name(f".{destination.stem}.{uuid.uuid4().hex}{destination.suffix}"); command[-1]=str(temp)
    try:
        proc=subprocess.run(command,capture_output=True,text=True,check=False)
        if proc.returncode: raise MediaError(f"ffmpeg mux failed: {proc.stderr.strip()}")
        os.link(temp,destination)
    finally: temp.unlink(missing_ok=True)


def register_routes(app: Any) -> None:
    def endpoint(fn):
        def wrapped():
            body=request.get_json(silent=True) or {}
            try:return fn(body)
            except (MediaError,OSError,subprocess.SubprocessError) as exc:return jsonify({"error":"MEDIA_ERROR","detail":str(exc)}),422
        wrapped.__name__=fn.__name__; return wrapped
    app.add_url_rule("/media/probe","media_probe",endpoint(lambda b:jsonify(probe_media(b.get("path","")))),methods=["POST"])
    app.add_url_rule("/media/decode_audio","media_decode_audio",endpoint(lambda b:_decode_response(b)),methods=["POST"])
    app.add_url_rule("/media/export_fcpxml","media_export_fcpxml",endpoint(lambda b:_export_response(export_fcpxml(b.get("project") or {},b.get("output_path",""),b.get("stems")))),methods=["POST"])
    app.add_url_rule("/media/export_aaf","media_export_aaf",endpoint(lambda b:_export_response(export_aaf(b.get("project") or {},b.get("output_path",""),b.get("stems")))),methods=["POST"])
    app.add_url_rule("/media/assemble_video","media_assemble_video",endpoint(lambda b:_export_response(assemble_video(b.get("video_path",""),b.get("audio_path",""),b.get("output_path","")))),methods=["POST"])


def _decode_response(body):
    import base64
    decoded=decode_audio(body.get("path",""),body.get("sample_rate",24000)); pcm=decoded.pop("audio")
    return jsonify({**decoded,"audio_base64":base64.b64encode(pcm).decode("ascii")})


def _export_response(path: Path): return jsonify({"ok":True,"path":str(path),"size_bytes":path.stat().st_size})
