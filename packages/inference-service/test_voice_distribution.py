import hashlib
import http.server
import importlib.util
import json
from pathlib import Path
import threading
import zipfile
import pytest
from flask import Flask

from inference_service import voice_distribution as client

SERVER_FILE = Path(__file__).resolve().parents[2] / "apps" / "voice-distribution" / "server.py"
spec = importlib.util.spec_from_file_location("svs_voice_distribution_server", SERVER_FILE)
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


@pytest.fixture
def live_service(tmp_path, monkeypatch):
    monkeypatch.setenv("SVS_DISTRIBUTION_HOME", str(tmp_path / "service"))
    server._schema()
    server._signing_key()
    server._create_user("admin", "fixture-admin-password-5482", "admin")
    server._create_user("member", "fixture-member-password-5482")
    archive = tmp_path / "self-owned.zip"
    terms = tmp_path / "self-owned-terms.txt"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("Fixture/README.txt", "Self-authored fixture; contains no model or third-party assets.")
    terms.write_text("Self-authored fixture terms for local service test only.\n", encoding="utf-8")
    monkeypatch.setenv("SVS_DISTRIBUTION_ACTOR", "admin")
    package_id, length, digest = server._add_package("Own fixture", "1", archive, "Self-owned test data. No commercial rights are granted.", terms)
    handler = server.make_handler()
    httpd = server.http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{httpd.server_port}"
    try:
        yield {"endpoint": endpoint, "package_id": package_id, "length": length, "digest": digest, "archive": archive, "terms": terms}
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def login(endpoint, username="member", password="fixture-member-password-5482"):
    status, _, body = client._request(endpoint, "/api/login", method="POST", payload={"username": username, "password": password})
    assert status == 200
    return json.loads(body)["access_token"]


def test_endpoint_requires_explicit_secure_trust():
    assert client._validate_endpoint("http://127.0.0.1:8765") == "http://127.0.0.1:8765"
    assert client._validate_endpoint("https://voices.example/base") == "https://voices.example/base"
    for endpoint in ("http://voices.example", "https://user:pass@voices.example", "https://voices.example/a/../b", "file:///tmp/voices"):
        with pytest.raises(client.DistributionError):
            client._validate_endpoint(endpoint)


def test_real_service_auth_pending_review_terms_and_release(live_service, monkeypatch, tmp_path):
    endpoint = live_service["endpoint"]
    with pytest.raises(client.DistributionError, match="HTTP 401"):
        client._request(endpoint, "/api/packages")
    token = login(endpoint)
    assert json.loads(client._request(endpoint, "/api/packages", token=token)[2])["packages"] == []
    with pytest.raises(client.DistributionError, match="HTTP 404"):
        client._request(endpoint, f"/api/download-token?id={live_service['package_id']}", token=token)

    server._admin("admin", "fixture-admin-password-5482")
    server._review(live_service["package_id"], "approved", "Verified self-authored fixture evidence")
    manifest = json.loads(client._request(endpoint, "/api/packages", token=token)[2])["packages"]
    item = manifest[0]
    assert item["terms_sha256"] == hashlib.sha256(live_service["terms"].read_bytes()).hexdigest()
    status, headers, body = client._request(endpoint, item["terms_url"], token=token)
    assert status == 200
    assert headers["x-content-sha256"] == item["terms_sha256"]
    assert body == live_service["terms"].read_bytes()

    download_token = json.loads(client._request(endpoint, f"/api/download-token?id={item['id']}", token=token)[2])["token"]
    local = tmp_path / "fixture.part"
    status, headers, _ = client._request(endpoint, f"/api/download/{item['id']}", headers={"X-Download-Token": download_token, "Range": "bytes=0-7"})
    assert status == 206 and headers["content-range"] == f"bytes 0-7/{item['byte_length']}"
    client._download({"endpoint": endpoint}, token, item, local)
    assert local.read_bytes() == live_service["archive"].read_bytes()
    # A completed cache is hash-verified and reused rather than requesting an invalid EOF range.
    client._download({"endpoint": endpoint}, token, item, local)
    assert hashlib.sha256(local.read_bytes()).hexdigest() == item["sha256"]


def test_download_rejects_corrupt_full_cache_without_using_it(live_service, tmp_path):
    endpoint = live_service["endpoint"]
    token = login(endpoint)
    server._admin("admin", "fixture-admin-password-5482")
    server._review(live_service["package_id"], "approved", "Verified fixture")
    item = json.loads(client._request(endpoint, "/api/packages", token=token)[2])["packages"][0]
    target = tmp_path / "corrupt.part"
    target.write_bytes(b"x" * item["byte_length"])
    client._download({"endpoint": endpoint}, token, item, target)
    assert hashlib.sha256(target.read_bytes()).hexdigest() == item["sha256"]

def test_download_error_does_not_truncate_resume_cache(tmp_path):
    import pytest

    payload = b"denied response body"
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(403)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        def log_message(self, *_):
            pass

    httpd = server.http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    target = tmp_path / "resume.part"
    original = b"previously downloaded bytes"
    target.write_bytes(original)
    try:
        with target.open("ab") as output, pytest.raises(client.DistributionError, match="HTTP 403"):
            client._request(f"http://127.0.0.1:{httpd.server_port}", "/api/download/x", headers={"Range": "bytes=28-"}, sink=output)
        assert target.read_bytes() == original
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_distribution_routes_require_explicit_json_acknowledgement(tmp_path, monkeypatch):
    monkeypatch.setenv("SVS_DISTRIBUTION_CLIENT_STATE_DIR", str(tmp_path / "separate-profile"))
    app = Flask(__name__)
    client.register_routes(app)
    http = app.test_client()
    assert http.get("/voices/distribution/status").status_code == 200
    assert http.post("/voices/distribution/disconnect", data="").status_code == 415
    assert http.post("/voices/distribution/install", json={"package_id": "a" * 32}).status_code == 400


def test_changed_approved_license_or_archive_requires_new_acceptance(live_service, tmp_path, monkeypatch):
    monkeypatch.setenv("SVS_DISTRIBUTION_CLIENT_STATE_DIR", str(tmp_path / "consent-profile"))
    monkeypatch.setattr(client, "_JOB", None)
    client.configure(live_service["endpoint"], "member", "fixture-member-password-5482")
    server._admin("admin", "fixture-admin-password-5482")
    server._review(live_service["package_id"], "approved", "Own fixture reviewed")
    item = client.list_packages()["packages"][0]
    app = Flask(__name__)
    client.register_routes(app)
    http = app.test_client()
    accepted = {"package_id": item["id"], "accepted_terms": True,
                "accepted_terms_sha256": item["terms_sha256"], "accepted_archive_sha256": item["sha256"]}
    with server._connect() as db:
        db.execute("UPDATE packages SET terms_sha256=? WHERE id=?", ("0" * 64, item["id"]))
    response = http.post("/voices/distribution/install", json=accepted)
    assert response.status_code == 422
    assert client.snapshot()["job"] is None
    with server._connect() as db:
        db.execute("UPDATE packages SET terms_sha256=?,sha256=? WHERE id=?", (item["terms_sha256"], "1" * 64, item["id"]))
    response = http.post("/voices/distribution/install", json=accepted)
    assert response.status_code == 422
    assert client.snapshot()["job"] is None
