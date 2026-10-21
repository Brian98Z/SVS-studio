"""Safe manual WebDAV sync for portable SVS project bundles."""
from __future__ import annotations

import base64
import ctypes
import hashlib
import http.client
import importlib.util
import json
import os
import re
import shutil
import socket
import tempfile
import threading
import time
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

MAX_ARCHIVE = 2 * 1024 * 1024 * 1024
MAX_PROPFIND = 2 * 1024 * 1024
CHUNK = 1024 * 1024
_LOCK = threading.RLock()
_NAME = re.compile(r"^[^/\\\\<>:\"|?*%#\x00-\x1f\x7f]+\.svsbundle$", re.IGNORECASE)


class SyncError(Exception):
    def __init__(self, detail, status=422):
        super().__init__(detail)
        self.status = status


def _state_dir():
    override = os.environ.get("SVS_SYNC_STATE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "SVS Studio" / "project-sync"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "svs-studio" / "project-sync"


class _FileLock:
    def __enter__(self):
        self.path = _state_dir() / "state.lock"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        if os.name == "nt":
            import msvcrt
            self.file.seek(0)
            if not self.file.read(1):
                self.file.write(b"0"); self.file.flush()
            self.file.seek(0); msvcrt.locking(self.file.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(self.file.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *_):
        if os.name == "nt":
            import msvcrt
            self.file.seek(0); msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        self.file.close()


def _protect(data):
    if os.name != "nt":
        raise SyncError("Secure credential storage is unavailable on this platform", 422)
    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]
    protect = ctypes.windll.crypt32.CryptProtectData
    protect.argtypes = [ctypes.POINTER(BLOB), ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(BLOB)]
    protect.restype = ctypes.c_int
    raw = ctypes.create_string_buffer(data)
    source = BLOB(len(data), ctypes.cast(raw, ctypes.POINTER(ctypes.c_ubyte)))
    result = BLOB()
    if not protect(ctypes.byref(source), "SVS Studio WebDAV", None, None, None, 0x1, ctypes.byref(result)):
        raise SyncError("Windows credential protection failed", 422)
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(result.pbData)


def _unprotect(data):
    if os.name != "nt":
        raise SyncError("Secure credential storage is unavailable on this platform", 422)
    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]
    unprotect = ctypes.windll.crypt32.CryptUnprotectData
    unprotect.argtypes = [ctypes.POINTER(BLOB), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(BLOB)]
    unprotect.restype = ctypes.c_int
    raw = ctypes.create_string_buffer(data)
    source = BLOB(len(data), ctypes.cast(raw, ctypes.POINTER(ctypes.c_ubyte)))
    result = BLOB()
    if not unprotect(ctypes.byref(source), None, None, None, None, 0x1, ctypes.byref(result)):
        raise SyncError("Stored credential cannot be decrypted", 422)
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(result.pbData)


def _read_state():
    p = _state_dir() / "profile.json"
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(obj, dict): return {}
        return obj
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise SyncError("Sync profile is unreadable", 422)


def _save_state(state):
    folder = _state_dir(); folder.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".profile-", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
            json.dump(state, out, ensure_ascii=False); out.flush(); os.fsync(out.fileno())
        os.replace(name, folder / "profile.json")
    finally:
        Path(name).unlink(missing_ok=True)


def _validate_url(value):
    if not isinstance(value, str) or len(value) > 2048: raise SyncError("Invalid WebDAV collection URL")
    u = urllib.parse.urlsplit(value)
    local = u.hostname in {"localhost", "127.0.0.1", "::1"}
    if u.scheme != "https" and not (u.scheme == "http" and local): raise SyncError("WebDAV URL must use HTTPS (HTTP is allowed only for literal loopback)")
    if not u.hostname or u.username is not None or u.password is not None or u.query or u.fragment: raise SyncError("Invalid WebDAV collection URL")
    try: _ = u.port
    except ValueError: raise SyncError("Invalid WebDAV collection URL")
    path = urllib.parse.unquote(u.path)
    if "\\" in path or any(x in {".", ".."} for x in path.split("/")): raise SyncError("Invalid WebDAV collection path")
    return urllib.parse.urlunsplit((u.scheme, u.netloc, urllib.parse.quote(path.rstrip("/") + "/", safe="/!$&'()+,;=:@-._~"), "", ""))


def _load_bundle():
    configured = os.environ.get("SVS_INTERCHANGE_BRIDGE")
    parent = Path(configured).expanduser().resolve().parent if configured else Path(__file__).resolve().parents[2] / "interchange"
    path = parent / "project_bundle.py"
    spec = importlib.util.spec_from_file_location("svs_sync_project_bundle", path)
    if spec is None or spec.loader is None: raise SyncError("Project bundle support is unavailable", 502)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


def _profile_credentials(state):
    try:
        credentials = json.loads(_unprotect(base64.b64decode(state["credential"], validate=True)))
        return credentials["username"], credentials["password"]
    except SyncError: raise
    except Exception: raise SyncError("Stored credential is invalid", 422)


def _target(url, name):
    if not isinstance(name, str) or len(name) > 130 or not _NAME.fullmatch(name) or name in {".", ".."} or name.endswith((".", " ")):
        raise SyncError("remote_name must be a safe .svsbundle basename")
    return urllib.parse.urlsplit(urllib.parse.urljoin(url, urllib.parse.quote(name, safe=".-_")))


def _request(url, method, headers, username, password, source=None, sink=None, limit=MAX_PROPFIND, payload=None):
    parsed = urllib.parse.urlsplit(url)
    cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    conn = cls(parsed.hostname, parsed.port, timeout=30, **({"context": __import__("ssl").create_default_context()} if parsed.scheme == "https" else {}))
    path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    auth = base64.b64encode((username + ":" + password).encode("utf-8")).decode("ascii")
    reqheaders = {**headers, "Authorization": "Basic " + auth, "Connection": "close"}
    try:
        conn.putrequest(method, path, skip_accept_encoding=True)
        for k, v in reqheaders.items(): conn.putheader(k, v)
        if source is not None: conn.putheader("Content-Length", str(source.stat().st_size))
        if payload is not None: conn.putheader("Content-Length", str(len(payload)))
        conn.endheaders()
        if payload is not None: conn.send(payload)
        if source is not None:
            with source.open("rb") as stream:
                while True:
                    block = stream.read(CHUNK)
                    if not block: break
                    conn.send(block)
        response = conn.getresponse()
        if sink is not None and response.status != 200:
            return response.status, {k.casefold(): v for k, v in response.getheaders()}, b""
        if sink is not None:
            size = 0
            close_sink = isinstance(sink, (str, os.PathLike))
            output = Path(sink).open("wb") if close_sink else sink
            try:
                while True:
                    block = response.read(CHUNK)
                    if not block: break
                    size += len(block)
                    if size > limit: raise SyncError("Remote bundle exceeds size limit", 502)
                    output.write(block)
            finally:
                if close_sink: output.close()
            body = b""
        else:
            if limit <= 0:
                body = b""
            else:
                body = response.read(limit + 1)
                if len(body) > limit: raise SyncError("WebDAV response exceeds size limit", 502)
        return response.status, {k.casefold(): v for k, v in response.getheaders()}, body
    except (socket.timeout, OSError, http.client.HTTPException) as exc:
        raise SyncError("WebDAV request failed", 502) from exc
    finally: conn.close()


def _status_error(status):
    if status in (401, 403): raise SyncError("WebDAV credentials were rejected", 401)
    raise SyncError("WebDAV server returned an unexpected response", 502)


def _strong_etag(headers):
    tag = headers.get("etag")
    if not tag or tag.startswith("W/") or not (tag.startswith('"') and tag.endswith('"')):
        return None
    return tag


def _list(state):
    url = state["url"]; user, password = _profile_credentials(state)
    body = b"<?xml version='1.0'?><d:propfind xmlns:d='DAV:'><d:prop><d:getetag/><d:getcontentlength/><d:getlastmodified/><d:resourcetype/></d:prop></d:propfind>"
    status, headers, data = _request(url, "PROPFIND", {"Depth": "1", "Content-Type": "application/xml"}, user, password, limit=MAX_PROPFIND, payload=body)
    if status not in (207, 200): _status_error(status)
    try: root = ET.fromstring(data)
    except ET.ParseError as exc: raise SyncError("WebDAV listing was malformed", 502) from exc
    result=[]
    for node in root.findall("{DAV:}response"):
        href = node.findtext("{DAV:}href")
        if not href: continue
        parsed = urllib.parse.urlsplit(urllib.parse.urljoin(url, href))
        base = urllib.parse.urlsplit(url)
        if (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc): continue
        path = urllib.parse.unquote(parsed.path)
        base_path = urllib.parse.unquote(base.path)
        if not path.startswith(base_path) or path.rstrip("/") == base_path.rstrip("/") or path.count("/") != base_path.count("/"): continue
        name = path.rstrip("/")[len(base_path.rstrip("/"))+1:]
        if not name or "/" in name or not name.lower().endswith(".svsbundle") or not _NAME.fullmatch(name): continue
        props = {}
        for propstat in node.findall("{DAV:}propstat"):
            status_line = (propstat.findtext("{DAV:}status") or "").split()
            if len(status_line) < 2 or status_line[1] != "200": continue
            prop = propstat.find("{DAV:}prop")
            if prop is not None:
                props.update((child.tag, child) for child in prop)
        if not props: continue
        resource_type = props.get("{DAV:}resourcetype")
        if resource_type is not None and resource_type.find("{DAV:}collection") is not None: continue
        props = {name: props["{DAV:}" + name].text for name in
                 ("getetag", "getcontentlength", "getlastmodified") if "{DAV:}" + name in props}
        etag=props.get("getetag")
        try: size=int(props["getcontentlength"]) if props.get("getcontentlength") else 0
        except ValueError: size=0
        result.append({"name":name,"etag":etag,"size":size,"modified":props.get("getlastmodified")})
    return result

def sync_api(body):
    if not isinstance(body, dict): raise SyncError("Request must be an object")
    action=body.get("action")
    with _LOCK, _FileLock():
        state=_read_state()
        if action == "configure":
            url=_validate_url(body.get("url")); user=body.get("username"); password=body.get("password")
            if not isinstance(user,str) or not user or len(user)>512 or not isinstance(password,str) or not password or len(password)>4096: raise SyncError("Username and password are required")
            credential=_protect(json.dumps({"username":user,"password":password},ensure_ascii=False).encode())
            same_profile = state.get("url") == url and state.get("username") == user
            candidate={"url":url,"username":user,"credential":base64.b64encode(credential).decode("ascii"),"bases":state.get("bases",{}) if same_profile else {}}
            _list(candidate)
            state=candidate
            _save_state(state)
            return {"ok":True,"configured":True,"url":url,"username":user,"credential_stored":True}
        if action == "disconnect":
            _save_state({}); return {"ok":True,"configured":False,"credential_stored":False}
        if action == "status":
            configured=bool(state.get("url") and state.get("credential"))
            return {"ok":True,"configured":configured,"url":state.get("url"),"username":_profile_credentials(state)[0] if configured else None,"credential_stored":configured}
        if action == "list":
            if not state.get("url"): raise SyncError("WebDAV is not configured")
            return {"ok":True,"items":_list(state),"url":state["url"]}
        raise SyncError("action must be status, configure, list, or disconnect")


def _project_base(body):
    value=body.get("project_base")
    if value: return Path(value).expanduser().resolve()
    path=body.get("project_path")
    if path: return Path(path).expanduser().resolve().parent
    return None


def _backup_current(body, bundle, state):
    project=body.get("project"); project_path=body.get("project_path")
    if project is None and not project_path: return None
    root=_state_dir()/"backups"; root.mkdir(parents=True,exist_ok=True)
    target=root/(time.strftime("%Y%m%d-%H%M%S")+"-"+uuid.uuid4().hex+".svsbundle")
    req={"path":str(target),"project_base":str(_project_base(body)) if _project_base(body) else None}
    if project is not None: req["project"]=project
    else: req["project_path"]=project_path
    bundle.export_bundle(req)
    return str(target)


def _verified_upload(url, local, condition, user, password):
    status, headers, _ = _request(url, "PUT", {"Content-Type": "application/octet-stream", **condition}, user, password, source=local, limit=0)
    if status in (409, 412):
        raise SyncError("Remote version changed during upload", 409)
    if status not in (200, 201, 204):
        _status_error(status)
    etag = _strong_etag(headers)
    if not etag:
        status, headers, _ = _request(url, "HEAD", {}, user, password, limit=0)
        if status != 200:
            raise SyncError("Remote upload read-back failed", 502)
        etag = _strong_etag(headers)
    if not etag:
        raise SyncError("Remote server omitted a strong ETag", 502)
    with tempfile.TemporaryDirectory(prefix="svs-verify-") as td:
        verified = Path(td) / "uploaded.svsbundle"
        status, headers, _ = _request(url, "GET", {"If-Match": etag}, user, password, sink=verified, limit=MAX_ARCHIVE)
        if status != 200 or _strong_etag(headers) != etag or _hash_file(local) != _hash_file(verified):
            raise SyncError("Remote upload checksum or version mismatch", 502)
    return etag


def _conflict_upload(state, name, local, user, password):
    conflict = name[:-10] + " (conflict " + uuid.uuid4().hex[:8] + ").svsbundle"
    url = urllib.parse.urlunsplit(_target(state["url"], conflict))
    etag = _verified_upload(url, local, {"If-None-Match": "*"}, user, password)
    return {"ok": True, "status": "conflict", "remote_name": name, "conflict_remote_name": conflict, "etag": etag}


def interchange_sync(body):
    if not isinstance(body,dict): raise SyncError("Request must be an object")
    action=body.get("action")
    with _LOCK, _FileLock():
        state=_read_state()
        if not state.get("url"): raise SyncError("WebDAV is not configured")
        user,password=_profile_credentials(state); bundle=_load_bundle()
        name=body.get("remote_name")
        target=_target(state["url"],name)
        url=urllib.parse.urlunsplit(target)
        bases=state.setdefault("bases",{})
        if action == "sync_push":
            with tempfile.TemporaryDirectory(prefix="svs-sync-") as td:
                local=Path(td)/name
                req={"path":str(local),"project_base":str(_project_base(body)) if _project_base(body) else None}
                if body.get("project") is not None: req["project"]=body["project"]
                elif body.get("project_path"): req["project_path"]=body["project_path"]
                else: raise SyncError("project or project_path is required")
                bundle.export_bundle(req)
                prior=bases.get(name)
                head_status, head_headers, _ = _request(url,"HEAD",{},user,password,limit=0)
                exists=head_status == 200
                if head_status not in (200,404): _status_error(head_status)
                current=_strong_etag(head_headers) if exists else None
                if exists and (not prior or not current or current != prior):
                    return _conflict_upload(state, name, local, user, password)
                condition = {"If-Match": prior} if exists else {"If-None-Match": "*"}
                try:
                    etag = _verified_upload(url, local, condition, user, password)
                except SyncError as exc:
                    if exc.status == 502:
                        check_status, check_headers, _ = _request(url, "HEAD", {}, user, password, limit=0)
                        changed = check_status == 200 and _strong_etag(check_headers) != prior
                        if not changed:
                            raise
                    elif exc.status != 409:
                        raise
                    return _conflict_upload(state, name, local, user, password)
                bases[name]=etag; _save_state(state)
                return {"ok":True,"status":"uploaded","remote_name":name,"etag":etag}
        if action == "sync_pull":
            fd,tmpname=tempfile.mkstemp(prefix="svs-download-",suffix=".svsbundle"); os.close(fd); downloaded=Path(tmpname)
            try:
                status,hdr,_=_request(url,"GET",{},user,password,sink=downloaded,limit=MAX_ARCHIVE)
                if status != 200: _status_error(status)
                etag=_strong_etag(hdr)
                if not etag: raise SyncError("Remote bundle has no strong ETag",502)
                destination=Path(body.get("destination") or "").expanduser().resolve()
                if not body.get("destination"): raise SyncError("destination is required")
                if destination.exists() or destination.is_symlink(): raise SyncError("destination already exists",409)
                backup=_backup_current(body,bundle,state)
                result=bundle.import_bundle({"path":str(downloaded),"destination":str(destination)})
                bases[name]=etag; _save_state(state)
                return {"ok":True,"status":"downloaded","remote_name":name,"etag":etag,"backup_path":backup,**result}
            finally: downloaded.unlink(missing_ok=True)
        raise SyncError("action must be sync_push or sync_pull")


def _hash_file(path):
    digest=hashlib.sha256()
    with Path(path).open("rb") as f:
        while True:
            block=f.read(CHUNK)
            if not block: break
            digest.update(block)
    return digest.hexdigest()


def register_routes(app):
    from flask import jsonify, request
    @app.post("/sync")
    def sync_route():
        if not request.is_json:
            return jsonify({"error":"SYNC_ERROR","detail":"Content-Type must be application/json"}),415
        try: return jsonify(sync_api(request.get_json(force=False)))
        except SyncError as exc: return jsonify({"error":"SYNC_ERROR","detail":str(exc)}),exc.status
        except (ValueError,KeyError,TypeError,OSError) as exc: return jsonify({"error":"SYNC_ERROR","detail":str(exc)}),422
