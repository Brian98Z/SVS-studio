"""Realtime mixer protocol boundary invariants; no native plugins are loaded."""
import unittest
import numpy as np

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
        response = self.client.post("/vst/realtime/process", data=b"", content_type="application/octet-stream",
                                    headers={"X-Audio-Session-Id": "not-a-session", "X-Audio-Sequence": "0"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json["error"], "SESSION_NOT_FOUND")

    def test_process_rejects_legacy_json_payload(self):
        response = self.client.post("/vst/realtime/process", json={"session_id": "not-a-session", "sequence": 0, "tracks": []})
        self.assertEqual(response.status_code, 415)
        self.assertEqual(response.json["error"], "INVALID_REQUEST")

    def test_binary_track_order_and_rejection_preserve_sequence(self):
        opened = self.client.post('/vst/realtime/open', json={
            'sample_rate': 48000, 'block_frames': 16,
            'tracks': [{'id': 'a', 'volume': .5}, {'id': 'b', 'volume': 2}],
        })
        self.assertEqual(opened.status_code, 200)
        sid = opened.json['session_id']
        headers = {'X-Audio-Session-Id': sid, 'X-Audio-Sequence': '0'}
        a = np.tile(np.array([.25, -.5], dtype='<f4'), (16, 1))
        b = np.tile(np.array([-.125, .375], dtype='<f4'), (16, 1))
        payload = a.tobytes() + b.tobytes()
        def process(data, sequence='0'):
            return self.client.post('/vst/realtime/process', data=data,
                content_type='application/octet-stream', headers={**headers, 'X-Audio-Sequence': sequence})
        try:
            self.assertEqual(process(payload, '1').status_code, 400)
            self.assertEqual(process(payload[:-4]).status_code, 400)
            invalid = a.copy(); invalid[0, 0] = np.nan
            self.assertEqual(process(invalid.tobytes() + b.tobytes()).status_code, 400)
            response = process(payload)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, 'application/octet-stream')
            self.assertEqual(response.headers['X-Audio-Sequence'], '0')
            self.assertEqual(response.headers['X-Audio-Frames'], '16')
            np.testing.assert_allclose(np.frombuffer(response.data, '<f4').reshape(16, 2), a*.5+b*2, atol=1e-7)
            self.assertEqual(process(payload).status_code, 400)
            self.assertEqual(process(payload, '1').status_code, 200)
        finally:
            self.client.post('/vst/realtime/close', json={'session_id': sid})

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
