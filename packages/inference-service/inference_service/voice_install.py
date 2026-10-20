"""Pinned official voicebank download, full-archive installation and registration.

No arbitrary URLs, credentials, model execution, or independently licensed dependencies.
"""
from __future__ import annotations

import copy
import hashlib
import http.client
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

from flask import jsonify, request

from . import voices

_CATALOG = {
    "qixuan-2.7.0": {
        "id": "qixuan-2.7.0", "name": "绮萱", "version": "2.7.0",
        "bytes": 420707627,
        "sha256": "fe8ee7c95883d327a2b7facbbe832a7a53e1344918dc791ca83fc23b08034110",
        "source_url": "https://github.com/yqzhishen/qixuan-diffsinger/releases/download/v2.7.0/Qixuan_v2.7.0_DiffSinger_OpenUtau.zip",
        "terms_url": "https://github.com/yqzhishen/qixuan-diffsinger/blob/main/terms_of_use/Terms_of_Use.zh-CN.md",
        "notice": "作者免费提供完整声库；请阅读并遵守原使用条款。原包全部保留，不修改模型/形象/条款。独立声码器及外部推理环境不包含，不自动下载；其许可须另行确认。安装不等于可推理或取得所有商业用途授权。",
    },
}
_LOCK = threading.RLock()
_JOB = None
_MAX_MEMBERS = 10000
_MAX_EXPANDED = 4 * 1024**3
_MAX_MEMBER = 1024**3
_BAD_PART = re.compile(r'[<>:"|?*\x00-\x1f\x7f]')
_RESERVED = re.compile(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', re.I)


def _install_root():
    if os.environ.get("SVS_VOICE_INSTALL_DIR"):
        return Path(os.environ["SVS_VOICE_INSTALL_DIR"]).expanduser().resolve()
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) if os.name == "nt" else Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return base / "SVS Studio" / "voicebanks"


def _progress(**values):
    with _LOCK:
        _JOB.update(values)


def _snapshot():
    with _LOCK:
        return {"job": copy.deepcopy(_JOB)}


class _OfficialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != "https" or parsed.hostname not in {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"} or parsed.username or parsed.password:
            raise ValueError("官方源返回了未审核的下载重定向")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _package_path(item, root):
    return root / ".downloads" / (item["id"] + "-" + item["sha256"] + ".part")


def _download(item, target):
    """Retain interrupted bytes; append only an exactly matching HTTP range."""
    target.parent.mkdir(parents=True, exist_ok=True)
    size = target.stat().st_size if target.exists() else 0
    if size > item["bytes"]:
        target.unlink()
        size = 0
    digest = hashlib.sha256()
    if size:
        with target.open("rb") as saved:
            while block := saved.read(1024 * 1024):
                digest.update(block)
    _progress(downloaded_bytes=size, resumed_bytes=size)
    if size < item["bytes"]:
        opener = urllib.request.build_opener(_OfficialRedirect())
        headers = {"User-Agent": "SVS-Studio-VoiceInstaller/1", "Accept-Encoding": "identity"}
        if size:
            headers["Range"] = f"bytes={size}-"
        req = urllib.request.Request(item["source_url"], headers=headers)
        try:
            with opener.open(req, timeout=30) as response:
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise ValueError("官方源返回了不支持的压缩传输，已保留断点")
                if response.status == 206:
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                    if not match or tuple(map(int, match.groups())) != (size, item["bytes"] - 1, item["bytes"]):
                        raise ValueError("官方源续传范围不符，已保留断点，拒绝拼接")
                elif response.status == 200:
                    # Range ignored: replace only after validating the response headers.
                    pass
                else:
                    raise ValueError("官方源未返回完整下载或有效续传")
                expected = item["bytes"] - size if response.status == 206 else item["bytes"]
                declared = response.headers.get("Content-Length")
                if declared is not None and int(declared) != expected:
                    raise ValueError("官方原包大小已变化或续传长度不符，已保留断点")
                if response.status == 200:
                    size = 0
                    digest = hashlib.sha256()
                    _progress(downloaded_bytes=0, resumed_bytes=0)
                with target.open("ab" if size else "wb") as output:
                    while True:
                        block = response.read(1024 * 1024)
                        if not block:
                            break
                        if size + len(block) > item["bytes"]:
                            raise ValueError("下载超过审核大小，已保留断点")
                        output.write(block)
                        output.flush()
                        digest.update(block)
                        size += len(block)
                        _progress(downloaded_bytes=size)
        except (OSError, urllib.error.URLError, http.client.HTTPException) as exc:
            raise ValueError("官方源下载中断，已保留断点；点击继续下载，未使用镜像或跳过TLS校验") from exc
    if size != item["bytes"]:
        raise ValueError("原包长度不足，已保留断点；点击继续下载，完成后仍校验SHA-256")
    if digest.hexdigest() != item["sha256"]:
        target.unlink()
        _progress(downloaded_bytes=0, resumed_bytes=0)
        raise ValueError("原包SHA-256不符，已删除损坏缓存，请重新下载")


def _extract(archive_path, destination):
    """Validate every member before writing; retain every original regular file."""
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        if not members or len(members) > _MAX_MEMBERS:
            raise ValueError("声库归档成员数量无效")
        seen = set()
        paths = []
        total = 0
        for info in members:
            raw = info.orig_filename
            parts = PurePosixPath(raw).parts
            mode = info.external_attr >> 16
            if (not parts or raw.startswith("/") or "\\" in raw or "\x00" in raw or
                    any(not p or p in {".", ".."} or p.endswith((".", " ")) or _BAD_PART.search(p) or _RESERVED.match(p) for p in raw.rstrip("/").split("/")) or
                    info.flag_bits & 1 or stat.S_ISLNK(mode) or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                raise ValueError("声库归档包含不安全路径或特殊文件")
            canonical = "/".join(parts).casefold()
            if canonical in seen:
                raise ValueError("声库归档包含重复路径")
            seen.add(canonical)
            total += info.file_size
            if info.file_size > _MAX_MEMBER or total > _MAX_EXPANDED:
                raise ValueError("声库展开大小超过限制")
            paths.append((info, parts))
        files = {"/".join(parts).casefold() for info, parts in paths if not info.is_dir()}
        if any("/".join(parts[:i]).casefold() in files for _, parts in paths for i in range(1, len(parts))):
            raise ValueError("声库归档文件与目录路径冲突")
        for info, parts in paths:
            path = destination.joinpath(*parts)
            if info.is_dir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, path.open("xb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)


def _run(item, root):
    owned = None
    try:
        root.mkdir(parents=True, exist_ok=True)
        package = _package_path(item, root)
        _download(item, package)
        with tempfile.TemporaryDirectory(prefix=".voice-install-", dir=root) as temporary:
            stage = Path(temporary)
            _progress(state="extracting")
            content = stage / "content"
            content.mkdir()
            _extract(package, content)
            configs = [p for p in content.rglob("dsconfig.yaml") if p.parent.name not in {"dsdur", "dspitch", "dsvariance"}]
            if len(configs) != 1:
                raise ValueError("原包未包含唯一声库根目录")
            bank = configs[0].parent
            if not (bank / "Terms_of_Use.zh-CN.pdf").is_file():
                raise ValueError("原包缺少使用条款，拒绝注册")
            relative = bank.relative_to(content)
            destination = root / item["id"]
            destination.mkdir()  # Atomic reservation: never replace an existing installation.
            owned = destination
            content.rename(destination / "content")
            installed = destination / "content" / relative
        _progress(state="registering")
        voice = voices.register_voice(str(installed), item["name"] + " " + item["version"])
        _progress(state="completed", destination=str(installed), voice=voice)
    except Exception as exc:
        if owned is not None:
            shutil.rmtree(owned, ignore_errors=True)
        # Network errors must never expose signed redirect URLs or credentials.
        detail = str(exc) if isinstance(exc, ValueError) else "声库安装失败；请检查目标目录权限、磁盘空间或归档完整性"
        _progress(state="failed", error=detail)


def register_routes(app):
    @app.get("/voices/catalog")
    def catalog():
        items = [{**item, "downloaded_bytes": min(_package_path(item, _install_root()).stat().st_size, item["bytes"]) if _package_path(item, _install_root()).is_file() else 0} for item in _CATALOG.values()]
        return jsonify({"items": items, "install_root": str(_install_root())})

    @app.get("/voices/install")
    def status():
        return jsonify(_snapshot())

    @app.post("/voices/install")
    def install():
        global _JOB
        if not request.is_json:
            return jsonify({"detail": "Content-Type must be application/json"}), 415
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or body.get("accepted_terms") is not True:
            return jsonify({"detail": "安装前必须明确同意作者使用条款"}), 422
        item = _CATALOG.get(body.get("catalog_id")) if isinstance(body.get("catalog_id"), str) else None
        if item is None:
            return jsonify({"detail": "未审核的声库目录条目"}), 422
        with _LOCK:
            if _JOB and _JOB["state"] not in {"completed", "failed"}:
                return jsonify({"detail": "已有声库安装正在进行"}), 409
            root = _install_root()
            if (root / item["id"]).exists():
                return jsonify({"detail": "声库安装目录已存在，不会覆盖；可扫描登记已有目录"}), 409
            _JOB = {"id": uuid.uuid4().hex, "catalog_id": item["id"], "state": "downloading", "downloaded_bytes": 0, "total_bytes": item["bytes"]}
            threading.Thread(target=_run, args=(copy.deepcopy(item), root), daemon=True).start()
            return jsonify(_snapshot()), 202
