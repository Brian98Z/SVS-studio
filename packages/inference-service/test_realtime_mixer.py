"""Realtime mixer protocol boundary invariants; no native plugins are loaded."""
import unittest

from flask import Flask

from inference_service.realtime_mixer import register_routes


class RealtimeMixerBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        register_routes(self.app)
        self.client = self.app.test_client()

    def test_open_rejects_invalid_block_size_before_spawning(self):
        response = self.client.post("/vst/realtime/open", json={
            "sample_rate": 48000,
            "block_frames": 0,
            "tracks": [{"id": "track-1"}],
            "mixer": {"master": {"volume": 1, "effects": []}, "buses": []},
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["error"], "INVALID_CONFIG")

    def test_process_rejects_unknown_session(self):
        response = self.client.post("/vst/realtime/process", json={
            "session_id": "not-a-session",
            "sequence": 0,
            "tracks": [],
        })
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json["error"], "SESSION_NOT_FOUND")

    def test_close_is_idempotent_for_unknown_session(self):
        response = self.client.post("/vst/realtime/close", json={"session_id": "not-a-session"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["ok"])

    def test_non_json_requests_are_rejected(self):
        response = self.client.post("/vst/realtime/open", data="{}", content_type="text/plain")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["error"], "INVALID_REQUEST")


if __name__ == "__main__":
    unittest.main()
