"""Self-hosted, review-gated voice package distribution service.

Only standard-library modules are used. Initialize and administer accounts and packages
with this module's CLI; the HTTP API never exposes administrative operations.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import http.server
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import ssl
import sys
import threading
import time
import urllib.parse
import uuid

CHUNK = 1024 * 1024
MAX_UPLOAD = 20 * 1024**3
TOKEN_TTL = 300


def _paths():
    root = Path(os.environ.get("SVS_DISTRIBUTION_HOME", Path.home() / ".svs-voice-distribution")).expanduser().resolve()
    return root, root / "distribution.sqlite3", root / "packages"


def _connect():
    _, db, _ = _paths()
    db.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def _schema():
    with _connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_salt BLOB NOT NULL,
            password_hash BLOB NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','user')),
            enabled INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS packages (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, version TEXT NOT NULL,
            filename TEXT NOT NULL, byte_length INTEGER NOT NULL, sha256 TEXT NOT NULL,
            rights_statement TEXT NOT NULL, terms_filename TEXT NOT NULL, terms_sha256 TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected')),
            submitted_by TEXT NOT NULL REFERENCES users(id), reviewed_by TEXT REFERENCES users(id),
            review_note TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL, reviewed_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash BLOB PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), expires_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at);
        CREATE INDEX IF NOT EXISTS packages_status ON packages(status);
        """)


def _password(password):
    if len(password) < 12:
        raise ValueError("Password must contain at least 12 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310_000)
    return salt, digest


def _create_user(username, password, role="user"):
    if role not in {"admin", "user"} or not username or len(username) > 128:
        raise ValueError("Invalid account name or role")
    salt, digest = _password(password)
    with _connect() as db:
        db.execute("INSERT INTO users(id,username,password_salt,password_hash,role,created_at) VALUES(?,?,?,?,?,?)",
                   (uuid.uuid4().hex, username, salt, digest, role, int(time.time())))


def _hash_file(path):
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while block := stream.read(CHUNK):
            size += len(block)
            if size > MAX_UPLOAD:
                raise ValueError(f"Package exceeds {MAX_UPLOAD} byte limit")
            digest.update(block)
    if size == 0:
        raise ValueError("Empty package")
    return size, digest.hexdigest()


def _add_package(title, version, archive, rights, terms):
    archive, terms = Path(archive).resolve(strict=True), Path(terms).resolve(strict=True)
    if not archive.is_file() or not terms.is_file():
        raise ValueError("Package and license/terms evidence must be regular files")
    if not title.strip() or not version.strip() or not rights.strip():
        raise ValueError("Title, version and explicit rights statement are required")
    size, digest = _hash_file(archive)
    terms_size, terms_digest = _hash_file(terms)
    if terms_size > 25 * 1024**2:
        raise ValueError("License/terms evidence exceeds 25 MiB")
    package_id = uuid.uuid4().hex
    root, _, store = _paths()
    directory = store / package_id
    directory.mkdir(parents=True, exist_ok=False)
    extension = archive.suffix if archive.suffix and len(archive.suffix) <= 16 and archive.suffix[1:].isalnum() else ".bin"
    package_name = "package" + extension
    terms_name = "license" + (terms.suffix[:16] if terms.suffix else ".txt")
    try:
        for source, target in ((archive, directory / package_name), (terms, directory / terms_name)):
            with source.open("rb") as src, target.open("xb") as dst:
                shutil.copyfileobj(src, dst, CHUNK)
                dst.flush()
                os.fsync(dst.fileno())
        with _connect() as db:
            user = db.execute("SELECT id FROM users WHERE username=? AND role='admin' AND enabled=1", (os.environ.get("SVS_DISTRIBUTION_ACTOR", ""),)).fetchone()
            # CLI actor is established only after validating an admin password.
            if not user:
                raise ValueError("Internal admin identity not established")
            db.execute("INSERT INTO packages(id,title,version,filename,byte_length,sha256,rights_statement,terms_filename,terms_sha256,status,submitted_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,'pending',?,?)",
                       (package_id, title.strip(), version.strip(), package_name, size, digest, rights.strip(), terms_name, terms_digest, user["id"], int(time.time())))
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return package_id, size, digest


def _admin(username, password):
    with _connect() as db:
        row = db.execute("SELECT id,password_salt,password_hash,role,enabled FROM users WHERE username=?", (username,)).fetchone()
    if not row or row["role"] != "admin" or not row["enabled"]:
        raise ValueError("Admin authentication failed")
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), row["password_salt"], 310_000)
    if not hmac.compare_digest(candidate, row["password_hash"]):
        raise ValueError("Admin authentication failed")
    os.environ["SVS_DISTRIBUTION_ACTOR"] = username
    return row["id"]


def _review(package_id, decision, note):
    with _connect() as db:
        actor = db.execute("SELECT id FROM users WHERE username=? AND role='admin' AND enabled=1", (os.environ.get("SVS_DISTRIBUTION_ACTOR", ""),)).fetchone()
        package = db.execute("SELECT * FROM packages WHERE id=?", (package_id,)).fetchone()
        if not actor or not package or package["status"] != "pending":
            raise ValueError("Admin identity missing, package absent, or package already reviewed")
        path = _paths()[2] / package_id
        blob, evidence = path / package["filename"], path / package["terms_filename"]
        if not blob.is_file() or not evidence.is_file() or _hash_file(blob) != (package["byte_length"], package["sha256"]) or _hash_file(evidence)[1] != package["terms_sha256"]:
            raise ValueError("Stored package or rights evidence failed integrity verification")
        db.execute("UPDATE packages SET status=?,reviewed_by=?,review_note=?,reviewed_at=? WHERE id=? AND status='pending'",
                   (decision, actor["id"], note.strip(), int(time.time()), package_id))


def _signing_key():
    _, db, _ = _paths()
    key_path = db.parent / "signing.key"
    key_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        key = key_path.read_bytes()
        if len(key) != 32:
            raise RuntimeError("Invalid signing.key; refusing to start")
        return key
    key = secrets.token_bytes(32)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
        f.flush()
        os.fsync(f.fileno())
    return key


def _token(key, payload):
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    encoded = __import__("base64").urlsafe_b64encode(body).rstrip(b"=")
    signature = hmac.new(key, encoded, hashlib.sha256).digest()
    return (encoded + b"." + __import__("base64").urlsafe_b64encode(signature).rstrip(b"=")).decode()


def _untoken(key, token):
    import base64
    try:
        body, sig = token.encode().split(b".", 1)
        expected = hmac.new(key, body, hashlib.sha256).digest()
        supplied = base64.urlsafe_b64decode(sig + b"=" * (-len(sig) % 4))
        if not hmac.compare_digest(expected, supplied):
            return None
        payload = json.loads(base64.urlsafe_b64decode(body + b"=" * (-len(body) % 4)))
        if not isinstance(payload, dict) or payload.get("exp", 0) < int(time.time()):
            return None
        return payload
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def make_handler():
    key = _signing_key()

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "SVSVoiceDistribution/1"
        def log_message(self, fmt, *args):
            # Never log bearer credentials, signed tokens, or query strings.
            sys.stderr.write("voice-distribution %s %s\n" % (self.address_string(), fmt.split("%", 1)[0]))

        def _json(self, status, data):
            encoded = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(encoded)

        def _body(self):
            try:
                length = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                return None
            if length < 0 or length > 16 * 1024:
                return None
            try:
                value = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return None
            return value if isinstance(value, dict) else None

        def _user(self):
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return None
            raw = auth[7:]
            digest = hashlib.sha256(raw.encode()).digest()
            with _connect() as db:
                row = db.execute("SELECT u.id,u.username,u.role FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>? AND u.enabled=1", (digest, int(time.time()))).fetchone()
            return row

        def do_POST(self):
            if self.path != "/api/login":
                self._json(404, {"error": "not_found"}); return
            body = self._body()
            if not body or not isinstance(body.get("username"), str) or not isinstance(body.get("password"), str):
                self._json(400, {"error": "invalid_request"}); return
            with _connect() as db:
                user = db.execute("SELECT id,password_salt,password_hash,enabled FROM users WHERE username=?", (body["username"],)).fetchone()
            if not user or not user["enabled"]:
                self._json(401, {"error": "invalid_credentials"}); return
            digest = hashlib.pbkdf2_hmac("sha256", body["password"].encode(), user["password_salt"], 310_000)
            if not hmac.compare_digest(digest, user["password_hash"]):
                self._json(401, {"error": "invalid_credentials"}); return
            token = secrets.token_urlsafe(32)
            expires = int(time.time()) + 12 * 3600
            with _connect() as db:
                db.execute("INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,?)", (hashlib.sha256(token.encode()).digest(), user["id"], expires))
            self._json(200, {"access_token": token, "token_type": "Bearer", "expires_at": expires})

        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path == "/api/packages":
                if not self._user():
                    self._json(401, {"error": "authentication_required"}); return
                with _connect() as db:
                    rows = db.execute("SELECT id,title,version,byte_length,sha256,rights_statement,terms_filename,terms_sha256 FROM packages WHERE status='approved' ORDER BY title,version").fetchall()
                items = []
                for row in rows:
                    item = dict(row)
                    item.pop("terms_filename", None)
                    item["terms_url"] = f"/api/packages/{item['id']}/terms"
                    items.append(item)
                self._json(200, {"packages": items}); return
            if parsed.path.startswith("/api/packages/") and parsed.path.endswith("/terms"):
                user = self._user()
                if not user:
                    self._json(401, {"error": "authentication_required"}); return
                package_id = parsed.path[len("/api/packages/"):-len("/terms")].strip("/")
                with _connect() as db:
                    package = db.execute("SELECT terms_filename,terms_sha256,status,id FROM packages WHERE id=? AND status='approved'", (package_id,)).fetchone()
                if not package:
                    self._json(404, {"error": "package_not_found"}); return
                evidence = _paths()[2] / package_id / package["terms_filename"]
                try:
                    evidence_size, evidence_hash = _hash_file(evidence)
                    if evidence_size > 25 * 1024**2 or evidence_hash != package["terms_sha256"]:
                        raise ValueError("evidence integrity failure")
                    with evidence.open("rb") as source:
                        content = source.read(25 * 1024**2 + 1)
                except (OSError, ValueError):
                    self._json(410, {"error": "terms_evidence_unavailable"}); return
                if len(content) != evidence_size:
                    self._json(410, {"error": "terms_evidence_unavailable"}); return
                extension = Path(package["terms_filename"]).suffix.casefold()
                content_type = {".pdf": "application/pdf", ".txt": "text/plain; charset=utf-8", ".md": "text/plain; charset=utf-8"}.get(extension, "application/octet-stream")
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(evidence_size))
                self.send_header("Content-Disposition", f'inline; filename="license{extension if extension in {".pdf", ".txt", ".md"} else ".bin"}"')
                self.send_header("X-Content-SHA256", evidence_hash)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "sandbox; default-src 'none'")
                self.send_header("Cache-Control", "private, no-store")
                self.end_headers()
                self.wfile.write(content)
                return
            if parsed.path == "/api/download-token":
                user = self._user()
                if not user:
                    self._json(401, {"error": "authentication_required"}); return
                package_id = urllib.parse.parse_qs(parsed.query).get("id", [""])[0]
                with _connect() as db:
                    package = db.execute("SELECT id FROM packages WHERE id=? AND status='approved'", (package_id,)).fetchone()
                if not package:
                    self._json(404, {"error": "package_not_found"}); return
                self._json(200, {"url": f"/api/download/{package_id}", "token": _token(key, {"pkg": package_id, "uid": user["id"], "exp": int(time.time()) + TOKEN_TTL}), "expires_in": TOKEN_TTL}); return
            if parsed.path.startswith("/api/download/"):
                package_id = parsed.path.removeprefix("/api/download/")
                signed = self.headers.get("X-Download-Token", "")
                payload = _untoken(key, signed)
                if not payload or payload.get("pkg") != package_id:
                    self._json(401, {"error": "invalid_or_expired_download_token"}); return
                with _connect() as db:
                    package = db.execute("SELECT * FROM packages WHERE id=? AND status='approved'", (package_id,)).fetchone()
                    user = db.execute("SELECT id FROM users WHERE id=? AND enabled=1", (payload.get("uid"),)).fetchone()
                if not package or not user:
                    self._json(404, {"error": "package_not_found"}); return
                file = _paths()[2] / package_id / package["filename"]
                try:
                    stat = file.stat()
                except OSError:
                    self._json(410, {"error": "package_unavailable"}); return
                try:
                    verified_size, verified_hash = _hash_file(file)
                except (OSError, ValueError):
                    self._json(410, {"error": "package_integrity_unavailable"}); return
                if verified_size != package["byte_length"] or verified_hash != package["sha256"]:
                    self._json(410, {"error": "package_integrity_unavailable"}); return
                start, end, status = 0, stat.st_size - 1, 200
                range_header = self.headers.get("Range")
                if range_header:
                    import re
                    match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
                    if not match or not (match.group(1) or match.group(2)):
                        self.send_error(416); return
                    left, right = match.groups()
                    if left:
                        start = int(left); end = int(right) if right else end
                    else:
                        suffix = int(right)
                        if suffix <= 0:
                            self.send_error(416); return
                        start = max(0, stat.st_size - suffix)
                    if start > end or start >= stat.st_size:
                        self.send_response(416); self.send_header("Content-Range", f"bytes */{stat.st_size}"); self.send_header("Content-Length", "0"); self.end_headers(); return
                    end = min(end, stat.st_size - 1); status = 206
                length = end - start + 1
                self.send_response(status)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Disposition", f'attachment; filename="{package["filename"]}"')
                self.send_header("X-Content-SHA256", package["sha256"])
                self.send_header("Cache-Control", "private, no-store")
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{stat.st_size}")
                self.end_headers()
                try:
                    with file.open("rb") as stream:
                        stream.seek(start)
                        remaining = length
                        while remaining:
                            block = stream.read(min(CHUNK, remaining))
                            if not block:
                                break
                            self.wfile.write(block)
                            remaining -= len(block)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            self._json(404, {"error": "not_found"})

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description="Self-hosted SVS voice distribution (admin-only local CLI)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Initialize persistent database; does not create a default account")
    create = sub.add_parser("create-admin", help="Create an administrator (password prompted securely)")
    create.add_argument("username")
    user = sub.add_parser("create-user", help="Create a user account (password prompted securely)")
    user.add_argument("username")
    add = sub.add_parser("submit", help="Store an original package and license evidence for review")
    add.add_argument("title"); add.add_argument("version"); add.add_argument("archive"); add.add_argument("--rights", required=True, help="Explicit rights assertion/limitations; not a license grant")
    add.add_argument("--terms", required=True, help="Path to license or other rights evidence")
    review = sub.add_parser("review", help="Approve or reject one pending package after human rights review")
    review.add_argument("package_id"); review.add_argument("decision", choices=("approved", "rejected")); review.add_argument("--note", required=True)
    sub.add_parser("pending", help="List pending package records")
    serve = sub.add_parser("serve", help="Start the HTTP service")
    serve.add_argument("--host", default="127.0.0.1"); serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--cert"); serve.add_argument("--key")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            _schema(); _signing_key(); print(f"Initialized {_paths()[1]}")
        elif args.command == "create-admin":
            import getpass
            _schema()
            with _connect() as db:
                has_admin = db.execute("SELECT 1 FROM users WHERE role='admin' LIMIT 1").fetchone() is not None
            if has_admin:
                _admin(input("Existing admin username: "), getpass.getpass("Existing admin password: "))
            password = getpass.getpass("New admin password (12+ chars): "); confirm = getpass.getpass("Confirm password: ")
            if password != confirm: raise ValueError("Passwords do not match")
            _create_user(args.username, password, "admin"); print("Administrator created")
        elif args.command == "create-user":
            import getpass
            _schema(); _admin(input("Admin username: "), getpass.getpass("Admin password: "))
            password = getpass.getpass("New user password (12+ chars): "); confirm = getpass.getpass("Confirm password: ")
            if password != confirm: raise ValueError("Passwords do not match")
            _create_user(args.username, password); print("User created")
        elif args.command in {"submit", "review", "pending"}:
            import getpass
            _schema(); username = input("Admin username: "); _admin(username, getpass.getpass("Admin password: "))
            if args.command == "submit":
                package_id, size, digest = _add_package(args.title, args.version, args.archive, args.rights, args.terms)
                print(f"Pending review: {package_id} bytes={size} sha256={digest}")
                print("Submission and evidence do not grant commercial use or redistribution rights.")
            elif args.command == "review":
                _review(args.package_id, args.decision, args.note); print(f"Package {args.decision}")
            else:
                with _connect() as db:
                    for row in db.execute("SELECT id,title,version,byte_length,sha256,rights_statement,terms_filename FROM packages WHERE status='pending' ORDER BY created_at"):
                        print(json.dumps(dict(row), ensure_ascii=False))
        else:
            _schema()
            if bool(args.cert) != bool(args.key): raise ValueError("--cert and --key must be supplied together")
            if args.host not in {"127.0.0.1", "::1", "localhost"} and not args.cert:
                raise ValueError("Non-loopback binding requires TLS certificate and key")
            server = http.server.ThreadingHTTPServer((args.host, args.port), make_handler())
            server.daemon_threads = True
            if args.cert:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.minimum_version = ssl.TLSVersion.TLSv1_2
                context.load_cert_chain(args.cert, args.key)
                server.socket = context.wrap_socket(server.socket, server_side=True)
            print(f"Listening on {args.host}:{args.port} ({'TLS' if args.cert else 'loopback HTTP'})", flush=True)
            try: server.serve_forever()
            except KeyboardInterrupt: pass
            finally: server.server_close()
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr); return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
