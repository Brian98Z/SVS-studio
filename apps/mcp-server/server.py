"""SVS Studio FastMCP Server（契约 §8）。

stdio 传输；所有工具打 http://127.0.0.1:8761/api/*。
启动: python server.py
"""
from __future__ import annotations

import asyncio
import json
import os

import httpx
from fastmcp import FastMCP

BASE = os.environ.get("SVS_SERVER", "http://127.0.0.1:8761/api")

mcp = FastMCP(
    "svs-studio",
    instructions=(
        "AI 歌声合成工作站。典型链路: "
        "svs_project_open/new → svs_note_add(写音符+歌词) → svs_curve_paint(画表现力) "
        "→ svs_synth(切片合成) → svs_render(导出 WAV)。"
    ),
)

_client = httpx.AsyncClient(timeout=300.0)


async def _api(method: str, path: str, body: dict | None = None) -> dict:
    r = await _client.request(method, BASE + path, json=body)
    data = r.json() if r.content else {}
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}: {json.dumps(data, ensure_ascii=False)}")
    return data


@mcp.tool
async def svs_health() -> str:
    """检查 SVS server 与推理服务存活状态。"""
    d = await _api("GET", "/health")
    return json.dumps(d, ensure_ascii=False)


@mcp.tool
async def svs_import_abc(path: str, voice: str = "", lyrics: str = "", key_shift: int = 0) -> str:
    """导入 ABC 曲谱生成演唱轨（复用 abc2diffsinger 解析）。

    path: .abc 文件绝对路径；voice: 多声部时指定声部名（如 Vocal）；
    lyrics: 演唱歌词（留空=全程「啊」）；key_shift: 整体升降半音。
    返回 track_id、音符数、BPM、拍号。
    """
    text = open(path, encoding="utf-8", errors="replace").read()
    body = {"abc": text, "key_shift": key_shift}
    if voice:
        body["voice"] = voice
    if lyrics:
        body["lyrics"] = lyrics
    d = await _api("POST", "/import/abc", body)
    return json.dumps(d, ensure_ascii=False)


@mcp.tool
async def svs_ai_lyrics(theme: str, n_notes: int, abc_path: str = "", voice: str = "") -> str:
    """AI 按音符数生成歌词（复用 abc2diffsinger LLM 管线，字数不符自动重试）。

    theme: 歌词主题；n_notes: 音符数；abc_path/voice 可选——提供时按谱面乐句分行。
    """
    body: dict = {"theme": theme, "n_notes": n_notes}
    if abc_path:
        body["abc"] = open(abc_path, encoding="utf-8", errors="replace").read()
        if voice:
            body["voice"] = voice
    d = await _api("POST", "/ai/lyrics", body)
    return json.dumps(d, ensure_ascii=False)


@mcp.tool
async def svs_project_open(path: str) -> str:
    """打开 .svsproj 工程文件。path 为绝对路径。"""
    d = await _api("POST", "/project/open", {"path": path})
    return json.dumps(d, ensure_ascii=False)


@mcp.tool
async def svs_project_save(path: str = "") -> str:
    """保存当前工程。省略 path 时保存到原位置。"""
    body = {"path": path} if path else {}
    d = await _api("POST", "/project/save", body)
    return json.dumps(d, ensure_ascii=False)


@mcp.tool
async def svs_track_add(kind: str, name: str = "") -> str:
    """添加轨道。kind: sing|instrument|audio|chord。"""
    d = await _api("POST", "/track", {"kind": kind, "name": name})
    return json.dumps(d, ensure_ascii=False)


@mcp.tool
async def svs_note_add(track: str, tick: int, dur: int, key: int, lyric: str = "") -> str:
    """向轨道添加音符。tick/dur 单位 tick（480=1拍）；key 为 MIDI 0-127（60=C4）；lyric 支持汉字/假名/英文。"""
    d = await _api("POST", f"/track/{track}/note",
                   {"tick": tick, "duration": dur, "key": key, "lyric": lyric})
    return json.dumps(d, ensure_ascii=False)


@mcp.tool
async def svs_note_list(track: str) -> str:
    """列出轨道全部音符（id/tick/dur/key/歌词）。"""
    d = await _api("GET", "/project")
    for t in d.get("tracks", []):
        if t["id"] == track or t["name"] == track:
            notes = [
                {"id": n["id"], "tick": n["tick"], "dur": n["duration"],
                 "key": n["key"], "lyric": n.get("lyric", "")}
                for n in t.get("notes", [])
            ]
            return json.dumps(notes, ensure_ascii=False)
    raise RuntimeError(f"track not found: {track}")


@mcp.tool
async def svs_curve_paint(track: str, param: str, from_tick: int, to_tick: int,
                         value: float | None, layer: str = "user") -> str:
    """绘制或擦除表现力曲线段。layer: user|envelope|direct|global（均可读写）；value 为 null 时擦除。pitch 单位音分(±1200)，其余 additive 层可用负值。"""
    d = await _api("POST", f"/track/{track}/curve",
                   {"param": param, "layer": layer, "from_tick": from_tick,
                    "to_tick": to_tick, "value": value})
    return json.dumps(d, ensure_ascii=False)


@mcp.tool
async def svs_curve_read(track: str, param: str, len: int = 0) -> str:
    """读取指定参数曲线的 base/user/envelope/direct/global 和 effective 层。"""
    suffix = f"?len={len}" if len > 0 else ""
    d = await _api("GET", f"/track/{track}/curve/{param}{suffix}")
    return json.dumps(d, ensure_ascii=False)


@mcp.tool
async def svs_synth(track: str = "") -> str:
    """触发切片合成（休止符切片 + LRU 缓存）。返回 done/total/cache_hits。"""
    body = {"track": track} if track else {}
    d = await _api("POST", "/synth", body)
    return json.dumps(d, ensure_ascii=False)


@mcp.tool
async def svs_render(out_path: str, from_ms: float = 0, to_ms: float = 0) -> str:
    """渲染导出 WAV。from_ms/to_ms 0 表示全程。"""
    body = {"path": out_path}
    if from_ms:
        body["from_ms"] = from_ms
    if to_ms:
        body["to_ms"] = to_ms
    d = await _api("POST", "/render", body)
    return json.dumps(d, ensure_ascii=False)


@mcp.tool
async def svs_transport(action: str, ms: float = 0) -> str:
    """控制已打开浏览器试听。action: play|stop|seek；seek 的 ms 为工程绝对毫秒。"""
    if action not in ("play", "stop", "seek"):
        raise ValueError("action must be play, stop or seek")
    d = await _api("POST", f"/transport/{action}", {"ms": ms} if action == "seek" else {})
    return json.dumps(d, ensure_ascii=False)


@mcp.tool
async def svs_voice_list() -> str:
    """列出已注册声库；返回描述符和可用能力，不包含凭据。"""
    return json.dumps(await _api("GET", "/inference/voices"), ensure_ascii=False)


@mcp.tool
async def svs_voice_inspect(path: str, name: str = "") -> str:
    """检查本地 DiffSinger 声库目录及说话人，不注册、不读取密钥。"""
    body = {"path": path}
    if name:
        body["name"] = name
    return json.dumps(await _api("POST", "/inference/voices/inspect", body), ensure_ascii=False)


@mcp.tool
async def svs_voice_register(path: str, name: str = "") -> str:
    """注册本地声库目录，不复制/删除声库文件。"""
    body = {"path": path}
    if name:
        body["name"] = name
    return json.dumps(await _api("POST", "/inference/voices", body), ensure_ascii=False)


@mcp.tool
async def svs_voice_remove(voice_id: str) -> str:
    """从声库注册表移除一个 ID；不会删除声库资产。"""
    return json.dumps(await _api("DELETE", f"/inference/voices/{voice_id}"), ensure_ascii=False)


@mcp.tool
async def svs_track_speaker(track: str, speaker_id: str) -> str:
    """为演唱轨设置 DiffSinger speaker_id。"""
    return json.dumps(await _api("PATCH", f"/track/{track}", {"voice": {"speaker_id": speaker_id}}), ensure_ascii=False)


@mcp.tool
async def svs_track_speaker_blend(track: str, blend: list[dict]) -> str:
    """设置说话人嵌入混合；blend 项须为 {speaker_id, weight}，权重约束由推理端校验。"""
    if not isinstance(blend, list) or not 1 <= len(blend) <= 8:
        raise ValueError("blend must contain 1-8 speaker_id/weight objects")
    return json.dumps(await _api("PATCH", f"/track/{track}", {"voice": {"blend": blend}}), ensure_ascii=False)


@mcp.tool
async def svs_tempo_set(tempos: list[dict]) -> str:
    """替换工程 tempo map，点位格式与工程 JSON tempo 对象相同。"""
    return json.dumps(await _api("PATCH", "/project/tempos", {"tempos": tempos}), ensure_ascii=False)


@mcp.tool
async def svs_expression_set(track: str, expression: dict) -> str:
    """批量设置指定音符的表达参数；expression 包含 note_ids 及 kind/vibrato/attack_ms/decay_ms。"""
    return json.dumps(await _api("POST", f"/track/{track}/expression", expression), ensure_ascii=False)


@mcp.tool
async def svs_mixer_set(mixer: dict) -> str:
    """替换工程 mixer JSON 配置，由服务器校验效果器、总线和发送。"""
    return json.dumps(await _api("PATCH", "/project/mixer", {"mixer": mixer}), ensure_ascii=False)


@mcp.tool
async def svs_interchange(action: str, path: str) -> str:
    """导入/导出 MIDI、MusicXML、UFDATA 或导出 LRC；action 使用 import_midi/export_midi 等。"""
    if action not in {"import_midi", "import_musicxml", "import_ufdata", "export_midi", "export_musicxml", "export_lrc"}:
        raise ValueError("unsupported interchange action")
    import base64
    from pathlib import Path
    body = {"action": action, "path": path}
    if action.startswith("import_"):
        source = Path(path)
        body["content"] = base64.b16encode(source.read_bytes()).decode("ascii") if action == "import_midi" else source.read_text(encoding="utf-8-sig")
    return json.dumps(await _api("POST", "/interchange", body), ensure_ascii=False)


@mcp.tool
async def svs_export_stems(directory: str) -> str:
    """将当前工程可听轨导出为独立 WAV stems 到本地目录。"""
    return json.dumps(await _api("POST", "/render/stems", {"directory": directory}), ensure_ascii=False)


@mcp.tool
async def svs_media_import(path: str, track_id: str = "", start_tick: int = 0) -> str:
    """将本地音频/视频文件的音频流作为音频片段导入工程。"""
    body = {"path": path, "start_tick": start_tick}
    if track_id:
        body["track_id"] = track_id
    return json.dumps(await _api("POST", "/media/import", body), ensure_ascii=False)


@mcp.tool
async def svs_media_export(format: str, output_path: str) -> str:
    """导出当前工程为 Final Cut Pro XML 或 AAF。"""
    routes = {"fcpxml": "/media/export/fcpxml", "aaf": "/media/export/aaf"}
    if format not in routes:
        raise ValueError("format must be fcpxml or aaf")
    return json.dumps(await _api("POST", routes[format], {"output_path": output_path}), ensure_ascii=False)


@mcp.tool
async def svs_media_assemble_video(video_path: str, audio_path: str, output_path: str) -> str:
    """使用本地媒体工具将视频与音频合成为新文件。"""
    return json.dumps(await _api("POST", "/media/assemble_video", {"video_path": video_path, "audio_path": audio_path, "output_path": output_path}), ensure_ascii=False)


@mcp.tool
async def svs_music_ai_status() -> str:
    """读取音乐 AI 功能可用状态及依赖前置条件，不返回密钥。"""
    return json.dumps(await _api("GET", "/inference/music-ai/status"), ensure_ascii=False)


@mcp.tool
async def svs_music_ai(operation: str, input_path: str, output_path: str = "", style: str = "piano accompaniment", model: str = "htdemucs") -> str:
    """音乐 AI：vocal-to-midi/stems 输入音频路径；score/accompaniment 输入 ABC 路径。返回结果 JSON，不包含服务端密钥。"""
    import base64
    from pathlib import Path

    routes = {"vocal-to-midi": "/inference/music-ai/vocal-to-midi", "stems": "/inference/music-ai/stems",
              "score": "/inference/music-ai/score", "accompaniment": "/inference/music-ai/accompaniment"}
    if operation not in routes:
        raise ValueError("operation must be vocal-to-midi, stems, score, or accompaniment")
    source = Path(input_path)
    if operation in {"score", "accompaniment"}:
        body = {"abc": source.read_text(encoding="utf-8")}
    else:
        body = {"audio_base64": base64.b64encode(source.read_bytes()).decode("ascii")}
    if operation == "stems":
        body["model"] = model
    if operation == "accompaniment":
        body["style"] = style
    result = await _api("POST", routes[operation], body)
    if output_path and operation == "vocal-to-midi" and result.get("midi_base64"):
        Path(output_path).write_bytes(base64.b64decode(result.pop("midi_base64")))
        result["midi_output_path"] = output_path
    elif output_path and operation == "stems" and isinstance(result.get("stems"), dict):
        destination = Path(output_path)
        destination.mkdir(parents=True, exist_ok=True)
        for name, stem in result["stems"].items():
            (destination / f"{name}.wav").write_bytes(base64.b64decode(stem["audio_base64"]))
        result["output_directory"] = str(destination)
        result["stems"] = list(result["stems"])
    elif output_path and operation in {"score", "accompaniment"} and result.get("abc"):
        Path(output_path).write_text(result["abc"], encoding="utf-8")
        result["output_path"] = output_path
    return json.dumps(result, ensure_ascii=False)


@mcp.tool
async def svs_get_status() -> str:
    """工程概况：名称/轨道/音符数/时长/引擎状态。"""
    d = await _api("GET", "/project")
    h = await _api("GET", "/health")
    tracks = [
        {"id": t["id"], "kind": t["kind"], "name": t["name"], "notes": len(t.get("notes", []))}
        for t in d.get("tracks", [])
    ]
    return json.dumps({"name": d["name"], "tracks": tracks, "inference": h.get("inference")},
                      ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()
