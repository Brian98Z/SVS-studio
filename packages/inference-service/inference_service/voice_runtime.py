"""Speaker selection and embedding blend for diffsinger_utau PredAll."""
from __future__ import annotations

import copy
import hashlib
import math
from typing import Any

import numpy as np

_COMPONENTS = ("dsdur", "dspitch", "dsvariance", "dsacoustic")


class _SpeakerEmbedding:
    """Small compatible SpkEmbededReader-like value object."""
    def __init__(self, name: str, embedding: np.ndarray):
        self.speaker_name = name
        self._embedding = np.asarray(embedding, dtype=np.float32).copy()

    def get_embed(self) -> np.ndarray:
        return self._embedding


def _speaker_tables(pred: Any) -> dict[str, dict[str, Any]]:
    tables: dict[str, dict[str, Any]] = {}
    for name in _COMPONENTS:
        component = getattr(pred, name, None)
        if component is None:
            raise ValueError(f"DiffSinger predictor is missing component {name}")
        speakers = getattr(component, "speakers", None) or []
        table: dict[str, Any] = {}
        for speaker in speakers:
            speaker_name = getattr(speaker, "speaker_name", None)
            if not isinstance(speaker_name, str) or not callable(getattr(speaker, "get_embed", None)):
                raise ValueError(f"{name} exposes an unsupported speaker object")
            if speaker_name in table:
                raise ValueError(f"{name} has duplicate speaker name {speaker_name!r}")
            table[speaker_name] = speaker
        tables[name] = table
    return tables


def _copy_with_speaker(pred: Any, alias: str, embedding: np.ndarray) -> Any:
    clone = copy.copy(pred)
    for name in _COMPONENTS:
        component = copy.copy(getattr(pred, name))
        speakers = list(getattr(component, "speakers", None) or [])
        speakers.append(_SpeakerEmbedding(alias, embedding))
        component.speakers = speakers
        setattr(clone, name, component)
    clone.available_speakers = list(getattr(pred, "available_speakers", []) or []) + [alias]
    return clone


def prepare_speaker(pred: Any, settings: dict[str, Any] | None) -> tuple[Any, str | None]:
    """Return (predictor, speaker-name argument) for predict_full_pipeline.

    settings accepts either {speaker_id: name} or {blend:[{speaker_id,weight}, ...]}.
    Blend embeddings require each speaker in all four predictor component maps with equal
    vector shapes; blend construction modifies only shallow component copies and leaves ONNX
    sessions and original predictor speaker lists untouched. Emotion parameters are rejected.
    """
    settings = settings or {}
    if not isinstance(settings, dict):
        raise ValueError("voice settings must be an object")
    if "emotion" in settings or "emotion_id" in settings:
        raise ValueError("This DiffSinger model has no configured emotion conditioning")
    blend = settings.get("blend")
    speaker_id = settings.get("speaker_id")
    if blend is None and speaker_id is None:
        return pred, None
    tables = _speaker_tables(pred)
    common = set.intersection(*(set(table) for table in tables.values()))
    if blend is None:
        speaker_name = str(speaker_id)
        if speaker_name not in common:
            raise ValueError(f"Unknown/incompatible speaker_id {speaker_name!r}; compatible IDs: {', '.join(sorted(common)) or '(none)'}")
        return pred, speaker_name
    if not isinstance(blend, list) or not 1 <= len(blend) <= 8:
        raise ValueError("blend must contain 1-8 speaker_id/weight entries")
    names: list[str] = []
    weights: list[float] = []
    for entry in blend:
        if not isinstance(entry, dict) or "speaker_id" not in entry or "weight" not in entry:
            raise ValueError("Each blend entry requires speaker_id and weight")
        name = str(entry["speaker_id"])
        weight = entry["weight"]
        if name not in common:
            raise ValueError(f"Speaker {name!r} is not available in every compatible model component")
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not math.isfinite(float(weight)) or float(weight) <= 0:
            raise ValueError("Blend weights must be finite positive numbers")
        names.append(name)
        weights.append(float(weight))
    if len(set(names)) != len(names):
        raise ValueError("Blend speaker IDs must be unique")
    if abs(sum(weights) - 1) > 1e-5:
        raise ValueError("Blend weights must sum to 1")
    embeddings = [np.asarray(tables["dsdur"][name].get_embed(), dtype=np.float32) for name in names]
    if any(value.ndim != 1 or value.shape != embeddings[0].shape or not np.isfinite(value).all() for value in embeddings):
        raise ValueError("Selected speaker embeddings have incompatible dimensions or invalid values")
    result = np.zeros_like(embeddings[0], dtype=np.float32)
    for embedding, weight in zip(embeddings, weights):
        result += embedding * weight
    identity = "|".join(f"{name}:{weight:.9g}" for name, weight in zip(names, weights))
    alias = "svs-blend-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    # Verify component embeddings too: all steps must use the same mathematically blended identity.
    for table in tables.values():
        component_vectors = [np.asarray(table[name].get_embed(), dtype=np.float32) for name in names]
        if any(value.shape != result.shape or not np.isfinite(value).all() for value in component_vectors):
            raise ValueError("Speaker embeddings differ across DiffSinger components")
        candidate = np.zeros_like(result)
        for embedding, weight in zip(component_vectors, weights):
            candidate += embedding * weight
        if not np.allclose(candidate, result, rtol=1e-5, atol=1e-6):
            raise ValueError("DiffSinger component speaker embeddings are not compatible")
    return _copy_with_speaker(pred, alias, result), alias
