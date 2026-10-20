"""Execute the stdlib score bridge in-process for packaged and development builds."""
from __future__ import annotations
import importlib.util
import os
from pathlib import Path


def _bridge():
    path = Path(os.environ.get('SVS_INTERCHANGE_BRIDGE', Path(__file__).resolve().parents[2] / 'interchange' / 'bridge.py'))
    spec = importlib.util.spec_from_file_location('svs_score_interchange', path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'Cannot load score bridge at {path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def register_routes(app):
    from flask import jsonify, request
    from .project_sync import SyncError, interchange_sync, register_routes as register_sync

    register_sync(app)

    @app.post('/interchange')
    def convert():
        try:
            if request.is_json:
                body = request.get_json(force=False)
                if isinstance(body, dict) and body.get('action') in ('sync_push', 'sync_pull'):
                    return jsonify(interchange_sync(body))
                return jsonify({'ok': True, **_bridge().dispatch(body)})
            return jsonify({'error': 'SYNC_ERROR', 'detail': 'Content-Type must be application/json'}), 415
        except SyncError as exc:
            return jsonify({'error': 'SYNC_ERROR', 'detail': str(exc)}), exc.status
        except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
            return jsonify({'error': 'INTERCHANGE_FAILED', 'detail': str(exc)}), 422
