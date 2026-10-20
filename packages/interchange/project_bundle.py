"""Portable project bundle import/export for the JSON bridge."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

MAX_ARCHIVE_SIZE = 2 * 1024 * 1024 * 1024
MAX_MEMBER_SIZE = 1024 * 1024 * 1024
MAX_TOTAL_SIZE = 4 * 1024 * 1024 * 1024
MAX_MEMBERS = 10000
BUNDLE_FORMAT = "svs-project-bundle"
BUNDLE_VERSION = 1


def _fail(message):
    raise ValueError(message)


def _hash_file(path):
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            size += len(block)
            if size > MAX_MEMBER_SIZE:
                _fail(f"payload exceeds size limit: {path.name}")
            digest.update(block)
    return size, digest.hexdigest()


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _warnings(project):
    warnings = []
    if any((t.get("singer_id") not in (None, "", "builtin.fallback") or t.get("instrument") or t.get("fx") or t.get("sends")) for t in project.get("tracks", []) if isinstance(t, dict)):
        warnings.append("Singer, instrument, effect, and plugin references are preserved but plugins and models are not included.")
    if project.get("mixer"):
        warnings.append("Mixer references are preserved but plugins and models are not included.")
    if any("vst3" in json.dumps(t, ensure_ascii=False).lower() for t in project.get("tracks", []) if isinstance(t, dict)):
        warnings.append("VST3 plugin references are preserved but plugin binaries are not included.")
    return warnings


def export_bundle(req):
    output = Path(req.get("path") or _fail("path is required")).expanduser()
    if output.exists():
        _fail("bundle output already exists")
    project = req.get("project")
    if project is None:
        source = Path(req.get("project_path") or _fail("project or project_path is required")).expanduser()
        project = json.loads(source.read_text(encoding="utf-8-sig"))
    if not isinstance(project, dict):
        _fail("project must be a JSON object")
    project = copy.deepcopy(project)
    project.setdefault("schema", 1)
    project.setdefault("name", "Imported project")
    project.setdefault("sample_rate", 24000)
    project.setdefault("ppq", 480)
    project.setdefault("tempos", [{"tick": 0, "bpm": 120.0}])
    project.setdefault("time_signatures", [{"tick": 0, "numerator": 4, "denominator": 4}])
    project.setdefault("tracks", [])
    project.setdefault("audio_clips", [])
    _validate_project(project)
    clips = project["audio_clips"]
    if not isinstance(clips, list):
        _fail("project.audio_clips must be an array")
    base_value = req.get("project_base")
    base = Path(base_value).expanduser().resolve() if base_value else None
    assets = {}
    sources_by_digest = {}
    entries = []
    for index, clip in enumerate(clips):
        if not isinstance(clip, dict) or not isinstance(clip.get("path"), str) or not clip["path"]:
            _fail(f"project.audio_clips[{index}].path must be a non-empty string")
        source = Path(clip["path"]).expanduser()
        if not source.is_absolute():
            if base is None:
                _fail("project_base is required when audio clip paths are relative")
            source = base / source
        source = source.resolve(strict=True)
        if not source.is_file():
            _fail(f"audio source is not a regular file: {clip['path']}")
        size, digest = _hash_file(source)
        member = sources_by_digest.get(digest)
        if member is None:
            suffix = source.suffix.lower()
            if suffix not in {".wav", ".wave", ".mp3", ".flac", ".ogg", ".oga", ".m4a", ".aac", ".opus", ".aiff", ".aif", ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpeg", ".mpg"}:
                _fail(f"unsupported audio source extension: {source.suffix}")
            member = f"media/{digest}{suffix}"
            sources_by_digest[digest] = member
            assets[member] = {"size": size, "sha256": digest}
            entries.append((member, source))
        clip["path"] = member
    project_data = _json_bytes(project)
    if len(entries) + 2 > MAX_MEMBERS or len(project_data) > MAX_MEMBER_SIZE or len(project_data) + sum(record["size"] for record in assets.values()) > MAX_TOTAL_SIZE:
        _fail("bundle exceeds member or total size limit")
    records = {"project.json": {"size": len(project_data), "sha256": hashlib.sha256(project_data).hexdigest()}, **assets}
    manifest = {"format": BUNDLE_FORMAT, "version": BUNDLE_VERSION, "payloads": records}
    manifest_data = _json_bytes(manifest)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            archive.writestr("project.json", project_data)
            archive.writestr("manifest.json", manifest_data)
            for member, source in entries:
                archive.write(source, member)
        if tmp.stat().st_size > MAX_ARCHIVE_SIZE:
            _fail("bundle archive exceeds size limit")
        # Hard-link publication is atomic and fails rather than replacing a raced-in destination.
        os.link(tmp, output)
    finally:
        tmp.unlink(missing_ok=True)
    return {"path": str(output.resolve()), "manifest": manifest, "warnings": _warnings(project)}


def _validate_project(project):
    if not isinstance(project, dict):
        _fail("bundle project must be a JSON object")
    for field in ("tempos", "time_signatures", "tracks", "audio_clips"):
        if field in project and not isinstance(project[field], list):
            _fail(f"project.{field} must be an array")
    if not isinstance(project.get("schema"), int) or not isinstance(project.get("name"), str):
        _fail("project schema and name are required")
    if not isinstance(project.get("sample_rate"), int) or project["sample_rate"] <= 0:
        _fail("project.sample_rate must be a positive integer")
    if type(project.get("ppq")) is not int or not 0 < project["ppq"] <= 0xffffffff:
        _fail("project.ppq must be a positive u32 integer")
    for field in ("tempos", "time_signatures", "tracks", "audio_clips"):
        if field not in project:
            _fail(f"project.{field} is required")
    if not project["tempos"] or not project["time_signatures"]:
        _fail("project tempos and time_signatures must not be empty")
    clips = project["audio_clips"]
    for index, clip in enumerate(clips):
        if not isinstance(clip, dict) or not all(isinstance(clip.get(key), str) for key in ("id", "track_id", "path")) or not isinstance(clip.get("start_tick"), int):
            _fail(f"project.audio_clips[{index}] is invalid")
    for index, point in enumerate(project["tempos"]):
        if not isinstance(point, dict) or not isinstance(point.get("tick"), int) or not isinstance(point.get("bpm"), (int, float)):
            _fail(f"project.tempos[{index}] is invalid")
        if type(point["tick"]) is not int or not 0 <= point["tick"] <= 0xffffffff or not 0 < point["bpm"] < float("inf"):
            _fail("tempo ticks must be u32 and BPM finite and positive")
        if (index == 0 and point["tick"] != 0) or (index and point["tick"] <= project["tempos"][index - 1]["tick"]):
            _fail("tempo table must start at zero with strictly increasing ticks")
        if point.get("curve", "step") not in ("step", "linear"):
            _fail("unsupported tempo curve")
    for index, point in enumerate(project["time_signatures"]):
        if not isinstance(point, dict) or not isinstance(point.get("tick"), int) or not isinstance(point.get("numerator"), int) or not isinstance(point.get("denominator"), int):
            _fail(f"project.time_signatures[{index}] is invalid")
    for index, track in enumerate(project["tracks"]):
        if not isinstance(track, dict) or not all(isinstance(track.get(key), str) for key in ("id", "kind", "name")) or track["kind"] not in {"sing", "instrument", "audio", "chord"}:
            _fail(f"project.tracks[{index}] is invalid")
        if not isinstance(track.get("notes", []), list) or not isinstance(track.get("curves", {}), dict):
            _fail(f"project.tracks[{index}] notes or curves are invalid")


def import_bundle(req):
    source = Path(req.get("path") or _fail("path is required")).expanduser()
    destination = Path(req.get("destination") or _fail("destination is required")).expanduser()
    if destination.exists() or destination.is_symlink():
        _fail("destination already exists")
    if not source.is_file() or source.stat().st_size > MAX_ARCHIVE_SIZE:
        _fail("bundle is missing or exceeds archive size limit")
    stage = None
    try:
        with zipfile.ZipFile(source, "r") as archive:
            infos = archive.infolist()
            if not infos or len(infos) > MAX_MEMBERS:
                _fail("bundle member count is invalid")
            names = [info.filename for info in infos]
            if len(set(names)) != len(names):
                _fail("bundle contains duplicate member names")
            total = 0
            for info in infos:
                name = info.filename
                path = PurePosixPath(name)
                mode = info.external_attr >> 16
                if path.is_absolute() or ".." in path.parts or "\\" in name or not name or stat.S_ISLNK(mode):
                    _fail(f"unsafe bundle member: {name}")
                if info.file_size > MAX_MEMBER_SIZE or info.file_size < 0:
                    _fail(f"bundle member exceeds size limit: {name}")
                total += info.file_size
                if total > MAX_TOTAL_SIZE or (info.file_size and info.file_size / max(info.compress_size, 1) > 1000):
                    _fail("bundle exceeds decompression limits")
            if "manifest.json" not in names or "project.json" not in names:
                _fail("bundle is missing manifest.json or project.json")
            manifest = json.loads(archive.read("manifest.json"))
            if not isinstance(manifest, dict) or manifest.get("format") != BUNDLE_FORMAT or manifest.get("version") != BUNDLE_VERSION or not isinstance(manifest.get("payloads"), dict):
                _fail("unsupported or malformed bundle manifest")
            payloads = manifest["payloads"]
            expected_names = set(payloads) | {"manifest.json"}
            if set(names) != expected_names or "project.json" not in payloads:
                _fail("bundle member set does not match manifest")
            for name, record in payloads.items():
                p = PurePosixPath(name)
                if not isinstance(name, str) or p.is_absolute() or ".." in p.parts or "\\" in name or ":" in name or name not in names:
                    _fail(f"unsafe or missing manifest payload: {name}")
                if name != "project.json" and (len(p.parts) != 2 or p.parts[0] != "media" or len(p.parts[1].split(".", 1)[0]) != 64 or any(c not in "0123456789abcdef" for c in p.parts[1].split(".", 1)[0]) or ("." in p.parts[1] and (len(p.parts[1].rsplit(".", 1)[1]) > 11 or not p.parts[1].rsplit(".", 1)[1].isalnum()))):
                    _fail(f"invalid bundle payload name: {name}")
                if not isinstance(record, dict) or set(record) != {"size", "sha256"} or not isinstance(record["size"], int) or record["size"] < 0 or not isinstance(record["sha256"], str) or len(record["sha256"]) != 64 or any(c not in "0123456789abcdef" for c in record["sha256"]):
                    _fail(f"invalid manifest record: {name}")
                digest = hashlib.sha256()
                size = 0
                if name == "project.json":
                    with archive.open(name, "r") as incoming:
                        data = incoming.read(record["size"] + 1)
                    size = len(data)
                    digest.update(data)
                else:
                    with archive.open(name, "r") as incoming:
                        while True:
                            block = incoming.read(1024 * 1024)
                            if not block:
                                break
                            size += len(block)
                            if size > record["size"]:
                                _fail(f"bundle payload length mismatch: {name}")
                            digest.update(block)
                if size != record["size"] or digest.hexdigest() != record["sha256"]:
                    _fail(f"bundle payload integrity check failed: {name}")
            with archive.open("project.json", "r") as project_stream:
                project = json.loads(project_stream.read(MAX_MEMBER_SIZE + 1))
            _validate_project(project)
            if any(clip["path"] not in payloads or clip["path"] == "project.json" for clip in project["audio_clips"]):
                _fail("project audio clip references a missing media payload")
            destination.parent.mkdir(parents=True, exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", suffix=".restore", dir=destination.parent))
            for name in payloads:
                target = stage.joinpath(*PurePosixPath(name).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name, "r") as incoming, target.open("wb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
            for clip in project["audio_clips"]:
                clip["path"] = str((destination / clip["path"]).resolve())
            (stage / "project.json").write_bytes(_json_bytes(project))
        # Publish the fully verified tree atomically, without replacing an existing directory.
        os.rename(stage, destination)
        stage = None
        return {"project": project, "project_path": str((destination / "project.json").resolve()), "path": str((destination / "project.json").resolve()), "directory": str(destination.resolve()), "manifest": manifest, "warnings": _warnings(project)}
    except (zipfile.BadZipFile, EOFError, OSError, RuntimeError) as exc:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)
        raise ValueError(f"invalid project bundle: {exc}") from exc
    except Exception:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)
        raise
