"""CSV parsing for OpenBCI-style EEG exports and the on-disk session cache."""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from django.conf import settings

CHANNEL_COL_RE = re.compile(r"eeg\s*channel\s*(\d+)", re.IGNORECASE)


class ParseError(ValueError):
    pass


def parse_csv(file_obj) -> dict:
    """Parse an OpenBCI-style CSV: Timestamp, EEG Channel 1..N, Marker tag/timestamp/value.

    Returns dict with: timestamps (s, float64 array starting at 0), channel_names,
    eeg (n_samples, n_channels) float64, fs (detected, Hz), markers (list of
    {tag, timestamp, value}).
    """
    try:
        df = pd.read_csv(file_obj, skip_blank_lines=True)
    except Exception as exc:  # noqa: BLE001
        raise ParseError(f"Não foi possível ler o CSV: {exc}") from exc

    df.columns = [c.strip() for c in df.columns]

    ts_col = next((c for c in df.columns if c.lower() == "timestamp"), None)
    if ts_col is None:
        raise ParseError("Coluna 'Timestamp' não encontrada no CSV.")

    channel_cols = []
    for c in df.columns:
        m = CHANNEL_COL_RE.match(c)
        if m:
            channel_cols.append((int(m.group(1)), c))
    if not channel_cols:
        raise ParseError("Nenhuma coluna 'EEG Channel N' encontrada no CSV.")
    channel_cols.sort(key=lambda t: t[0])
    channel_names = [f"Ch{idx}" for idx, _ in channel_cols]
    eeg_cols = [c for _, c in channel_cols]

    eeg = df[eeg_cols].apply(pd.to_numeric, errors="coerce").to_numpy(dtype=np.float64)
    raw_ts = pd.to_numeric(df[ts_col], errors="coerce").to_numpy(dtype=np.float64)

    valid = ~np.isnan(raw_ts) & ~np.isnan(eeg).any(axis=1)
    if valid.sum() < 2:
        raise ParseError("Menos de 2 amostras válidas após remover linhas incompletas.")
    raw_ts = raw_ts[valid]
    eeg = eeg[valid]

    diffs = np.diff(raw_ts)
    diffs = diffs[diffs > 0]
    if diffs.size == 0:
        raise ParseError("Não foi possível estimar a taxa de amostragem (timestamps não crescem).")
    median_dt_ms = float(np.median(diffs))
    fs = 1000.0 / median_dt_ms

    timestamps = (raw_ts - raw_ts[0]) / 1000.0  # seconds, relative to first sample

    markers = []
    tag_col = next((c for c in df.columns if c.lower() == "marker tag"), None)
    mts_col = next((c for c in df.columns if c.lower() == "marker timestamp"), None)
    mval_col = next((c for c in df.columns if c.lower() == "marker value"), None)
    if tag_col is not None:
        marker_rows = df.loc[valid, [c for c in [tag_col, mts_col, mval_col] if c]]
        for pos, (_, row) in enumerate(marker_rows.iterrows()):
            tag = row.get(tag_col)
            if pd.isna(tag) or str(tag).strip() == "":
                continue
            markers.append({
                "tag": str(tag),
                "timestamp": float(row.get(mts_col)) if mts_col and not pd.isna(row.get(mts_col)) else None,
                "value": str(row.get(mval_col)) if mval_col and not pd.isna(row.get(mval_col)) else None,
                "sample_time": float(timestamps[pos]),
            })

    return {
        "timestamps": timestamps,
        "channel_names": channel_names,
        "eeg": eeg,
        "fs": fs,
        "markers": markers,
    }


def cache_dir() -> Path:
    return Path(settings.SIGNALS_CACHE_DIR)


def save_session(
    parsed: dict,
    name: str | None = None,
    original_filename: str | None = None,
    participant_name: str | None = None,
    sex: str | None = None,
    age: str | None = None,
    is_eyes_test: bool = False,
) -> str:
    token = uuid.uuid4().hex
    path = cache_dir() / f"{token}.npz"
    np.savez_compressed(
        path,
        timestamps=parsed["timestamps"],
        eeg=parsed["eeg"],
        fs=np.array([parsed["fs"]]),
        channel_names=np.array(parsed["channel_names"]),
    )
    markers_path = cache_dir() / f"{token}.markers.json"
    markers_path.write_text(json.dumps(parsed.get("markers", [])), encoding="utf-8")

    age_val = None
    if age not in (None, ""):
        try:
            age_val = int(age)
        except (TypeError, ValueError):
            age_val = None

    meta = {
        "token": token,
        "name": (name or "").strip() or (original_filename or f"Teste {token[:6]}"),
        "original_filename": original_filename,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "fs": parsed["fs"],
        "n_samples": int(parsed["eeg"].shape[0]),
        "duration_s": float(parsed["timestamps"][-1]),
        "channel_names": parsed["channel_names"],
        "markers_count": len(parsed.get("markers", [])),
        # Participant metadata -- optional, for future demographic breakdowns.
        "participant_name": (participant_name or "").strip() or None,
        "sex": (sex or "").strip() or None,
        "age": age_val,
        # Explicit flag, not inferred from marker count: other protocols besides
        # the eyes-closed/eyes-open one will be recorded with this same tool,
        # and some of those may coincidentally also have 5 markers.
        "is_eyes_test": bool(is_eyes_test),
    }
    meta_path = cache_dir() / f"{token}.meta.json"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    return token


def list_tests() -> list[dict]:
    """Registry of every recording ever uploaded (persisted on disk, no TTL) --
    used by the multi-test comparison feature to let a student pick which
    recordings to compare without re-uploading them."""
    tests = []
    for meta_path in cache_dir().glob("*.meta.json"):
        try:
            tests.append(json.loads(meta_path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    tests.sort(key=lambda t: t.get("uploaded_at", ""), reverse=True)
    return tests


def delete_test(token: str) -> None:
    safe_token = _safe_token(token)
    for suffix in (".npz", ".markers.json", ".meta.json"):
        p = cache_dir() / f"{safe_token}{suffix}"
        if p.exists():
            p.unlink()


def _safe_token(token: str) -> str:
    safe_token = re.sub(r"[^a-f0-9]", "", token.lower())
    if not safe_token or safe_token != token.lower():
        raise ParseError("Token de sessão inválido.")
    return safe_token


def load_session(token: str) -> dict:
    safe_token = _safe_token(token)
    path = cache_dir() / f"{safe_token}.npz"
    if not path.exists():
        raise ParseError("Sessão expirada ou não encontrada. Faça upload do CSV novamente.")
    with np.load(path, allow_pickle=False) as data:
        result = {
            "timestamps": data["timestamps"],
            "eeg": data["eeg"],
            "fs": float(data["fs"][0]),
            "channel_names": list(data["channel_names"]),
        }
    markers_path = cache_dir() / f"{safe_token}.markers.json"
    result["markers"] = json.loads(markers_path.read_text(encoding="utf-8")) if markers_path.exists() else []

    meta_path = cache_dir() / f"{safe_token}.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    result["is_eyes_test"] = bool(meta.get("is_eyes_test", False))
    result["name"] = meta.get("name")
    result["participant_name"] = meta.get("participant_name")
    result["sex"] = meta.get("sex")
    result["age"] = meta.get("age")
    return result
