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
    @app.post('/interchange')
    def convert():
        try:
            body = request.get_json(force=True)
            return jsonify({'ok': True, **_bridge().dispatch(body)})
        except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
            return jsonify({'error': 'INTERCHANGE_FAILED', 'detail': str(exc)}), 422
