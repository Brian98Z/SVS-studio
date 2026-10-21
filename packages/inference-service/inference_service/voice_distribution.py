"""Explicitly configured, trusted self-hosted voice distribution client."""
from __future__ import annotations

import base64
import ctypes
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import tempfile
import threading
import time
import urllib.parse
import uuid
import zipfile

from flask import Blueprint, jsonify, request

from . import voice_install, voices

_PROFILE_REVISION = 0


_LOCK = threading.RLock()
_JOB = None
CHUNK = 1024 * 1024
MAX_MANIFEST = 2 * 1024 * 1024


class DistributionError(ValueError):
    pass


def _state_dir():
    override = os.environ.get("SVS_DISTRIBUTION_CLIENT_STATE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "SVS Studio" / "voice-distribution-client"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "svs-studio" / "voice-distribution-client"


def _state_path():
    return _state_dir() / "profile.json"


def _protect(data):
    if os.name != "nt":
        raise DistributionError("Secure credential storage is unavailable on this platform")
    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]
    protect = ctypes.windll.crypt32.CryptProtectData
    protect.argtypes = [ctypes.POINTER(BLOB), ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(BLOB)]
    protect.restype = ctypes.c_int
    raw = ctypes.create_string_buffer(data)
    source, result = BLOB(len(data), ctypes.cast(raw, ctypes.POINTER(ctypes.c_ubyte))), BLOB()
    if not protect(ctypes.byref(source), "SVS Studio voice distribution", None, None, None, 0x1, ctypes.byref(result)):
        raise DistributionError("Windows credential protection failed")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(result.pbData)


def _unprotect(data):
    if os.name != "nt":
        raise DistributionError("Secure credential storage is unavailable on this platform")
    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]
    unprotect = ctypes.windll.crypt32.CryptUnprotectData
    unprotect.argtypes = [ctypes.POINTER(BLOB), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(BLOB)]
    unprotect.restype = ctypes.c_int
    raw = ctypes.create_string_buffer(data)
    source, result = BLOB(len(data), ctypes.cast(raw, ctypes.POINTER(ctypes.c_ubyte))), BLOB()
    if not unprotect(ctypes.byref(source), None, None, None, None, 0x1, ctypes.byref(result)):
        raise DistributionError("Stored distribution credential cannot be decrypted")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(result.pbData)


def _read():
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise DistributionError("Distribution profile is unreadable")


def _save(state):
    directory = _state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".profile-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(state, stream, ensure_ascii=False)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(name, _state_path())
    finally:
        Path(name).unlink(missing_ok=True)


def _credentials():
    state = _read()
    try:
        secret = json.loads(_unprotect(base64.b64decode(state["credential"], validate=True)))
        return state, secret["username"], secret["access_token"]
    except DistributionError:
        raise
    except Exception:
        raise DistributionError("Distribution credentials are not configured")


def _validate_endpoint(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise DistributionError("Invalid trusted distribution endpoint")
    parsed = urllib.parse.urlsplit(value)
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
        raise DistributionError("Distribution endpoint must use HTTPS (HTTP only for literal loopback)")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise DistributionError("Invalid trusted distribution endpoint")
    try:
        port = parsed.port
    except ValueError:
        raise DistributionError("Invalid trusted distribution endpoint")
    path = urllib.parse.unquote(parsed.path).rstrip("/")
    if "\\" in path or any(part in {".", ".."} for part in path.split("/")):
        raise DistributionError("Invalid trusted distribution endpoint path")
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    netloc = host + (f":{port}" if port else "")
    return urllib.parse.urlunsplit((parsed.scheme, netloc, path, "", ""))


def _request(endpoint, path, method="GET", token=None, payload=None, headers=None, sink=None, limit=MAX_MANIFEST, expected=None):
    parsed = urllib.parse.urlsplit(endpoint)
    conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(parsed.hostname, parsed.port, timeout=30, **({"context": ssl.create_default_context()} if parsed.scheme == "https" else {}))
    raw_path, separator, query = path.partition("?")
    request_path = urllib.parse.urlunsplit(("", "", parsed.path.rstrip("/") + raw_path, query if separator else "", ""))
    request_headers = {"Accept-Encoding": "identity", "Connection": "close", **(headers or {})}
    if token:
        request_headers["Authorization"] = "Bearer " + token
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    try:
        conn.request(method, request_path, body=body, headers=request_headers)
        response = conn.getresponse()
        response_headers = {key.casefold(): value for key, value in response.getheaders()}
        if response_headers.get("content-encoding", "identity").lower() != "identity":
            raise DistributionError("Distribution server returned unsupported content encoding")
        if expected:
            if response.status != expected.get("status") or any(response_headers.get(name) != value for name, value in expected.get("headers", {}).items()):
                raise DistributionError("Download response range or integrity metadata did not match the approved manifest")
            if response_headers.get("content-length") != str(expected.get("length")):
                raise DistributionError("Download Content-Length differs from the approved package range")
        if response.status not in (200, 206):
            data = response.read(min(limit + 1, 64 * 1024))
            detail = "Distribution server request failed"
            if data:
                try: detail = json.loads(data).get("error", detail)
                except (ValueError, AttributeError): pass
            raise DistributionError(f"{detail} (HTTP {response.status})")
        if sink is None:
            data = response.read(limit + 1)
            if len(data) > limit:
                raise DistributionError("Distribution response exceeded its size limit")
        else:
            data = b""
            remaining = int(response_headers.get("content-length", "-1"))
            if remaining < 0:
                raise DistributionError("Download response omitted Content-Length")
            received = 0
            with sink:
                while remaining:
                    block = response.read(min(CHUNK, remaining))
                    if not block: break
                    sink.write(block)
                    remaining -= len(block)
                    received += len(block)
                    if expected and expected.get("progress"):
                        with _LOCK:
                            if _JOB and _JOB.get("state") == "downloading": _JOB["downloaded_bytes"] = expected["offset"] + received
            if remaining:
                raise DistributionError("Download ended before Content-Length")
        return response.status, response_headers, data
    except (OSError, http.client.HTTPException) as exc:
        raise DistributionError("Distribution server connection failed") from exc
    finally:
        if sink is not None and not sink.closed: sink.close()
        conn.close()

def configure(endpoint, username, password):
    global _PROFILE_REVISION
    endpoint = _validate_endpoint(endpoint)
    if not isinstance(username, str) or not username or len(username) > 128 or not isinstance(password, str) or not password:
        raise DistributionError("Username and password are required")
    _, _, body = _request(endpoint, "/api/login", method="POST", payload={"username": username, "password": password})
    try:
        token = json.loads(body)["access_token"]
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise DistributionError("Distribution login returned an invalid token")
    credential = base64.b64encode(_protect(json.dumps({"username": username, "access_token": token}).encode())).decode("ascii")
    with _LOCK:
        _save({"endpoint": endpoint, "username": username, "credential": credential})
        _PROFILE_REVISION += 1
    return {"configured": True, "endpoint": endpoint, "username": username}


def status():
    state = _read()
    return {"configured": bool(state.get("endpoint") and state.get("credential")), "endpoint": state.get("endpoint"), "username": state.get("username")}


def disconnect():
    global _PROFILE_REVISION
    with _LOCK:
        _save({})
        _PROFILE_REVISION += 1
    return {"configured": False}

def _require_profile_revision(revision):
    with _LOCK:
        if revision != _PROFILE_REVISION:
            raise DistributionError("Distribution credentials changed during installation; installation cancelled")


def _manifest():
    state, username, token = _credentials()
    _, _, body = _request(state["endpoint"], "/api/packages", token=token)
    try:
        value = json.loads(body)
        packages = value["packages"]
        if not isinstance(packages, list):
            raise ValueError()
        for item in packages:
            if (not isinstance(item, dict) or not re.fullmatch(r"[0-9a-f]{32}", item.get("id", "")) or
                    not isinstance(item.get("title"), str) or not isinstance(item.get("version"), str) or
                    type(item.get("byte_length")) is not int or item["byte_length"] <= 0 or
                    not re.fullmatch(r"[0-9a-f]{64}", item.get("sha256", "")) or
                    not isinstance(item.get("rights_statement"), str) or
                    not isinstance(item.get("terms_url"), str) or not re.fullmatch(r"[0-9a-f]{64}", item.get("terms_sha256", ""))):
                raise ValueError()
        return state, username, token, packages
    except (ValueError, KeyError, TypeError):
        raise DistributionError("Distribution manifest is invalid")


def list_packages():
    _, _, _, packages = _manifest()
    return {"packages": packages}


def _download(state, token, item, target):
    endpoint = state["endpoint"]
    _, _, body = _request(endpoint, f"/api/download-token?id={urllib.parse.quote(item['id'], safe='')}", token=token)
    try:
        signed = json.loads(body)["token"]
        if not isinstance(signed, str) or not signed:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise DistributionError("Distribution server returned an invalid signed download token")
    size = target.stat().st_size if target.exists() else 0
    if size > item["byte_length"]:
        target.unlink(); size = 0
    if size == item["byte_length"]:
        digest = hashlib.sha256()
        with target.open("rb") as source:
            while block := source.read(CHUNK): digest.update(block)
        if digest.hexdigest() == item["sha256"]: return
        target.unlink(); size = 0
    digest = hashlib.sha256()
    if size:
        with target.open("rb") as source:
            while block := source.read(CHUNK): digest.update(block)
    headers = {"X-Download-Token": signed}
    if size:
        headers["Range"] = f"bytes={size}-"
    expected_length = item["byte_length"] - size if size else item["byte_length"]
    expected_status = 206 if size else 200
    expected_range = f"bytes {size}-{item['byte_length'] - 1}/{item['byte_length']}" if size else None
    response_status, response_headers, _ = _request(endpoint, f"/api/download/{item['id']}", token=None, headers=headers, sink=target.open("ab" if size else "wb"), expected={"status": expected_status, "length": expected_length, "offset": size, "progress": True, "headers": {"x-content-sha256": item["sha256"], **({"content-range": expected_range} if expected_range else {})}})
    # Length and exact status/range/hash headers are checked before body bytes are accepted.
    if target.stat().st_size != item["byte_length"]:
        raise DistributionError("Downloaded archive is incomplete; resume can be retried")
    digest = hashlib.sha256()
    with target.open("rb") as stream:
        while block := stream.read(CHUNK): digest.update(block)
    if digest.hexdigest() != item["sha256"]:
        target.unlink(missing_ok=True)
        raise DistributionError("Downloaded archive SHA-256 mismatch; corrupt cache removed")


def _install(item, profile_revision):
    global _JOB
    try:
        _require_profile_revision(profile_revision)
        root = voice_install._install_root()
        root.mkdir(parents=True, exist_ok=True)
        if (root / item["id"]).exists():
            raise DistributionError("Voice package install directory already exists; refusing overwrite")
        download = root / ".downloads" / f"{item['id']}-{item['sha256']}.part"
        download.parent.mkdir(parents=True, exist_ok=True)
        state, _, token, packages = _manifest()
        current = next((p for p in packages if p["id"] == item["id"]), None)
        if current != item:
            raise DistributionError("Approved manifest changed since selection; refresh before installing")
        with _LOCK: _JOB.update(state="downloading", downloaded_bytes=download.stat().st_size if download.exists() else 0, total_bytes=item["byte_length"])
        _download(state, token, item, download)
        _require_profile_revision(profile_revision)
        with _LOCK: _JOB.update(downloaded_bytes=item["byte_length"])
        with tempfile.TemporaryDirectory(prefix=".voice-distribution-", dir=root) as temporary:
            content = Path(temporary) / "content"
            content.mkdir()
            with _LOCK: _JOB.update(state="extracting")
            voice_install._extract(download, content)
            configs = [p for p in content.rglob("dsconfig.yaml") if p.parent.name not in {"dsdur", "dspitch", "dsvariance"}]
            if len(configs) != 1:
                raise DistributionError("Archive must contain exactly one voicebank root")
            bank = configs[0].parent
            if not any(p.is_file() and p.name.casefold() in {"terms_of_use.zh-cn.pdf", "license", "license.txt", "license.md", "copying"} for p in bank.iterdir()):
                raise DistributionError("Archive voicebank must include its license/terms file")
            with _LOCK:
                _require_profile_revision(profile_revision)
                _JOB.update(state="registering")
                destination = root / item["id"]
                destination.mkdir()
                try:
                    (content).rename(destination / "content")
                    installed = destination / "content" / bank.relative_to(content)
                    voice = voices.register_voice(str(installed), f"{item['title']} {item['version']}")
                except Exception:
                    shutil.rmtree(destination, ignore_errors=True)
                    raise
        with _LOCK: _JOB.update(state="completed", destination=str(installed), voice=voice, downloaded_bytes=item["byte_length"])
    except Exception as exc:
        detail = str(exc) if isinstance(exc, (ValueError, DistributionError)) else "Voice distribution installation failed; check disk space, permissions, and archive integrity"
        with _LOCK: _JOB.update(state="failed", error=detail)
def start_install(package_id, accepted_terms, accepted_terms_sha256, accepted_archive_sha256):
    global _JOB, _PROFILE_REVISION
    if (not isinstance(package_id, str) or accepted_terms is not True or
            not isinstance(accepted_terms_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", accepted_terms_sha256) or
            not isinstance(accepted_archive_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", accepted_archive_sha256)):
        raise DistributionError("Explicit acceptance of the reviewed license and archive SHA-256 is required")
    with _LOCK:
        revision = _PROFILE_REVISION
    _, _, _, packages = _manifest()
    item = next((p for p in packages if p["id"] == package_id), None)
    if not item:
        raise DistributionError("Approved package not found")
    if item["terms_sha256"] != accepted_terms_sha256 or item["sha256"] != accepted_archive_sha256:
        raise DistributionError("Approved license or archive changed; refresh and review before installing")
    with _LOCK:
        _require_profile_revision(revision)
        if _JOB and _JOB.get("state") in {"downloading", "extracting", "registering"}:
            raise DistributionError("Another distribution install is active")
        _JOB = {"id": uuid.uuid4().hex, "package_id": package_id, "state": "downloading", "downloaded_bytes": 0, "total_bytes": item["byte_length"]}
        threading.Thread(target=_install, args=(item, revision), daemon=True).start()
        return dict(_JOB)


def get_terms(package_id):
    if not isinstance(package_id, str) or not re.fullmatch(r"[0-9a-f]{32}", package_id):
        raise DistributionError("Invalid package id")
    state, _, token, packages = _manifest()
    item = next((p for p in packages if p["id"] == package_id), None)
    if not item:
        raise DistributionError("Approved package not found")
    path = item["terms_url"]
    if not path.startswith("/api/packages/") or not path.endswith("/terms"):
        raise DistributionError("Terms evidence URL is invalid")
    status_code, headers, body = _request(state["endpoint"], path, token=token, limit=25 * 1024**2)
    digest = hashlib.sha256(body).hexdigest()
    if status_code != 200 or digest != item["terms_sha256"] or int(headers.get("content-length", "-1")) != len(body):
        raise DistributionError("Original license evidence failed length or SHA-256 verification")
    return {"filename": headers.get("content-disposition", ""), "content_type": headers.get("content-type", "application/octet-stream"), "sha256": digest, "content": base64.b64encode(body).decode("ascii")}


def snapshot():
    with _LOCK:
        return {"job": dict(_JOB) if _JOB else None}


def register_routes(app):
    """Register /voices/distribution/* API routes on the inference Flask app."""
    @app.get("/voices/distribution/status")
    def distribution_status():
        try: return jsonify({**status(), **snapshot()})
        except DistributionError as exc: return jsonify({"detail": str(exc)}), 422

    @app.get("/voices/distribution/terms/<package_id>")
    def distribution_terms(package_id):
        try: return jsonify(get_terms(package_id))
        except DistributionError as exc: return jsonify({"detail": str(exc)}), 422
    @app.post("/voices/distribution/configure")
    def distribution_configure():
        body = request.get_json(silent=True)
        if not isinstance(body, dict): return jsonify({"detail": "JSON object required"}), 400
        try: return jsonify(configure(body.get("endpoint"), body.get("username"), body.get("password")))
        except DistributionError as exc: return jsonify({"detail": str(exc)}), 422

    @app.post("/voices/distribution/disconnect")
    def distribution_disconnect():
        if not request.is_json: return jsonify({"detail": "application/json required"}), 415
        try: return jsonify(disconnect())
        except DistributionError as exc: return jsonify({"detail": str(exc)}), 422

    @app.get("/voices/distribution/catalog")
    def distribution_catalog():
        try: return jsonify(list_packages())
        except DistributionError as exc: return jsonify({"detail": str(exc)}), 502

    @app.post("/voices/distribution/install")
    def distribution_install():
        body = request.get_json(silent=True)
        if (not isinstance(body, dict) or not isinstance(body.get("package_id"), str) or body.get("accepted_terms") is not True or
                not isinstance(body.get("accepted_terms_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", body["accepted_terms_sha256"]) or
                not isinstance(body.get("accepted_archive_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", body["accepted_archive_sha256"])):
            return jsonify({"detail": "package_id, accepted_terms=true and reviewed license/archive SHA-256 are required"}), 400
        try: return jsonify(start_install(body["package_id"], True, body["accepted_terms_sha256"], body["accepted_archive_sha256"])), 202
        except DistributionError as exc: return jsonify({"detail": str(exc)}), 422

    @app.get("/voices/distribution/install")
    def distribution_install_status():
        return jsonify(snapshot())
