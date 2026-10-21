"""Browser-origin requests must be rejected before local service mutations."""
from inference_service import service_runner


def test_untrusted_browser_cannot_invalidate_audio_cache(monkeypatch):
    removed = []

    class Cache:
        def invalidate(self, hashes, all):
            removed.append((hashes, all))
            return 1

    monkeypatch.setattr(service_runner, "cache", Cache())
    monkeypatch.setattr(service_runner, "LOCAL_PORT", 8760)
    client = service_runner.app.test_client()
    for headers in ({"Origin": "https://attacker.invalid"}, {"Host": "attacker.invalid:8760"}, {"Sec-Fetch-Site": "cross-site"}):
        response = client.post("/invalidate", base_url="http://127.0.0.1:8760", json={"all": True}, headers=headers)
        assert response.status_code == 403
        assert removed == []
    response = client.post("/invalidate", base_url="http://127.0.0.1:8760", json={"all": True})
    assert response.status_code == 200
    assert removed == [(None, True)]
