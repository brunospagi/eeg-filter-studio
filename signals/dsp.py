"""Signal-processing helpers for the EEG filtering pipeline.

All filters are zero-phase (forward-backward, via sosfiltfilt) so that peaks in
the EEG are not shifted in time -- important when correlating filtered traces
against marker events.
"""
from __future__ import annotations

import numpy as np
from scipy import signal

# OpenBCI Cyton default gain (24) railed/saturated ADC reading, in microvolts.
# Vref(4.5V) / gain(24) / (2**23 - 1) * 1e6 * (2**23 - 1) ~= 187500 uV
DEFAULT_SATURATION_UV = 187500.0

# Runs of saturated samples shorter than this get linearly interpolated and
# treated as recovered; longer runs are still interpolated (so filtfilt has a
# well-behaved, continuous input) but flagged as "excluded" -- there is no real
# signal there, so the filtered output in that window should not be trusted.
DEFAULT_MAX_GAP_MS = 250.0

# Canonical EEG frequency bands (Hz). Gamma's upper edge is capped well below
# the 60 Hz mains notch so the band doesn't straddle the notch's attenuation
# dip -- 45 Hz is the conventional practical ceiling for scalp EEG gamma.
EEG_BANDS = {
    "theta": (4.0, 8.0),
    "alpha": (8.0, 13.0),
    "beta": (13.0, 30.0),
    "gamma": (30.0, 45.0),
}


class FilterError(ValueError):
    """Raised when the requested filter parameters are invalid for the data."""


def _validate_band(low, high, fs):
    nyquist = fs / 2.0
    if low is not None and not (0 < low < nyquist):
        raise FilterError(f"Corte inferior ({low} Hz) precisa estar entre 0 e {nyquist:.1f} Hz (Nyquist).")
    if high is not None and not (0 < high < nyquist):
        raise FilterError(f"Corte superior ({high} Hz) precisa estar entre 0 e {nyquist:.1f} Hz (Nyquist).")
    if low is not None and high is not None and low >= high:
        raise FilterError("Corte inferior precisa ser menor que o corte superior.")


def detrend(data: np.ndarray, kind: str) -> np.ndarray:
    """kind: 'none' | 'constant' (remove DC offset) | 'linear' (remove linear drift)."""
    if kind == "none":
        return data
    if kind not in ("constant", "linear"):
        raise FilterError(f"Tipo de detrend inválido: {kind}")
    return signal.detrend(data, axis=0, type=kind)


def bandpass(data: np.ndarray, fs: float, low: float, high: float, order: int) -> np.ndarray:
    _validate_band(low, high, fs)
    if not (1 <= order <= 8):
        raise FilterError("Ordem do filtro precisa estar entre 1 e 8.")
    sos = signal.butter(order, [low, high], btype="bandpass", fs=fs, output="sos")
    # Narrow low-frequency bands (theta, 4-8 Hz) need several cycles to settle;
    # scipy's default odd-reflection padding actually makes that edge
    # transient *larger* here (it extrapolates from a single boundary sample),
    # so we skip padding entirely -- empirically the smallest edge artifact of
    # the options tried. The transient this leaves behind at the very start/
    # end of the buffer is handled by excluding an edge margin from power
    # estimates (see `band_power_rms`), not by trying to pad it away.
    return signal.sosfiltfilt(sos, data, axis=0, padlen=0)


def highpass(data: np.ndarray, fs: float, cutoff: float, order: int) -> np.ndarray:
    _validate_band(cutoff, None, fs)
    sos = signal.butter(order, cutoff, btype="highpass", fs=fs, output="sos")
    return signal.sosfiltfilt(sos, data, axis=0)


def lowpass(data: np.ndarray, fs: float, cutoff: float, order: int) -> np.ndarray:
    _validate_band(cutoff, None, fs)
    sos = signal.butter(order, cutoff, btype="lowpass", fs=fs, output="sos")
    return signal.sosfiltfilt(sos, data, axis=0)


def notch(data: np.ndarray, fs: float, freq: float, q: float, harmonics: int = 1) -> np.ndarray:
    """Remove `freq` (e.g. 50/60 Hz mains hum) and, optionally, its harmonics."""
    nyquist = fs / 2.0
    out = data
    for h in range(1, max(1, harmonics) + 1):
        f0 = freq * h
        if f0 >= nyquist:
            break
        b, a = signal.iirnotch(f0, q, fs=fs)
        out = signal.filtfilt(b, a, out, axis=0)
    return out


def detect_saturation(raw: np.ndarray, threshold: float) -> np.ndarray:
    """Boolean mask (n_samples, n_channels) flagging samples at/near ADC rail."""
    return np.abs(raw) >= threshold


def _runs(mask_col: np.ndarray) -> list[tuple[int, int]]:
    """Contiguous (start, end) index pairs (end exclusive) where mask_col is True."""
    if not mask_col.any():
        return []
    edges = np.diff(mask_col.astype(np.int8))
    starts = list(np.where(edges == 1)[0] + 1)
    ends = list(np.where(edges == -1)[0] + 1)
    if mask_col[0]:
        starts = [0] + starts
    if mask_col[-1]:
        ends = ends + [mask_col.size]
    return list(zip(starts, ends))


def mask_to_time_ranges(mask_col: np.ndarray, timestamps: np.ndarray) -> list[list[float]]:
    """Contiguous True runs of a 1-D boolean mask as [t_start, t_end] pairs."""
    return [[float(timestamps[start]), float(timestamps[end - 1])] for start, end in _runs(mask_col)]


def interpolate_saturation(data: np.ndarray, mask: np.ndarray, max_gap_samples: int):
    """Linearly interpolate saturated runs so filters don't see a railed square wave.

    Runs longer than `max_gap_samples` are still interpolated (to keep the signal
    continuous for filtfilt) but also reported in the returned `excluded` mask,
    since there is no real signal to recover there.

    Returns (interpolated_data, excluded_mask).
    """
    out = data.copy()
    excluded = np.zeros_like(mask)
    n = data.shape[0]
    for j in range(data.shape[1]):
        for start, end in _runs(mask[:, j]):
            length = end - start
            left = out[start - 1, j] if start > 0 else None
            right = out[end, j] if end < n else None
            if left is None and right is None:
                continue  # channel is saturated end-to-end; nothing to anchor to
            if left is None:
                out[start:end, j] = right
            elif right is None:
                out[start:end, j] = left
            else:
                out[start:end, j] = np.linspace(left, right, length + 2)[1:-1]
            if length > max_gap_samples:
                excluded[start:end, j] = True
    return out, excluded


def decompose_bands(data: np.ndarray, fs: float, bands: dict | None = None, order: int = 4) -> dict:
    """Bandpass-filter `data` (n_samples, n_channels) into each named canonical
    band. A band whose upper edge would fall at/above Nyquist is clipped to
    (Nyquist - 0.5 Hz); a band left with no usable width is skipped.

    Returns {band_name: filtered_array}, each array shaped like `data`.
    """
    bands = bands or EEG_BANDS
    nyquist = fs / 2.0
    out = {}
    for name, (low, high) in bands.items():
        high_eff = min(high, nyquist - 0.5)
        if low >= high_eff:
            continue
        out[name] = bandpass(data, fs, low, high_eff, order)
    return out


# Zero-phase filtering a narrow band (esp. theta) leaves an edge transient at
# the very start/end of the buffer -- there is no real signal before sample 0
# for the filter to settle against, padding or not. Left in, it can dominate
# an RMS estimate by several times over (measured: ~7x on real data). Power
# estimates are computed excluding this margin from each end.
BAND_EDGE_MARGIN_S = 2.0


def band_power_rms(arr: np.ndarray, fs: float, margin_s: float = BAND_EDGE_MARGIN_S) -> float:
    """RMS of `arr` (1-D), excluding `margin_s` seconds of filter edge
    transient from each end. Falls back to the full array if it is too short
    for any margin to leave usable signal."""
    margin = int(round(margin_s * fs))
    if arr.size > 2 * margin + 1:
        arr = arr[margin:-margin]
    return float(np.sqrt(np.mean(arr ** 2)))


def band_power_by_markers(bands: dict, timestamps: np.ndarray, marker_times: list, fs: float) -> dict | None:
    """Split each band's per-channel power into eyes-closed vs. eyes-open
    conditions, using the lab's protocol: 5 marker events bounding 4
    alternating segments (closed1, open1, closed2, open2). The two closed
    (resp. open) segments are combined via RMS-of-RMS, same as the offline
    group-summary analysis, so the web app and the analysis script agree.

    Returns None when fewer than 5 markers are present -- there is nothing
    to segment. Otherwise {band_name: {"closed": array(n_channels), "open": array(n_channels)}}.
    """
    times = sorted(marker_times)
    if len(times) < 5:
        return None
    t0, t1, t2, t3, t4 = times[:5]
    bounds = {"closed1": (t0, t1), "open1": (t1, t2), "closed2": (t2, t3), "open2": (t3, t4)}

    def seg_rms(col: np.ndarray, bnd: tuple) -> float:
        i0, i1 = np.searchsorted(timestamps, bnd[0]), np.searchsorted(timestamps, bnd[1])
        return band_power_rms(col[i0:i1], fs)

    out = {}
    for band_name, arr in bands.items():
        n_channels = arr.shape[1]
        closed = np.empty(n_channels)
        opened = np.empty(n_channels)
        for ch in range(n_channels):
            col = arr[:, ch]
            c1, c2 = seg_rms(col, bounds["closed1"]), seg_rms(col, bounds["closed2"])
            o1, o2 = seg_rms(col, bounds["open1"]), seg_rms(col, bounds["open2"])
            closed[ch] = float(np.sqrt(np.mean([c1 ** 2, c2 ** 2])))
            opened[ch] = float(np.sqrt(np.mean([o1 ** 2, o2 ** 2])))
        out[band_name] = {"closed": closed, "open": opened}
    return out


def apply_band_pipeline(raw: np.ndarray, fs: float, params: dict) -> dict:
    """Clean the signal (saturation mitigation -> detrend -> notch, same as
    `apply_pipeline`) and then branch into the canonical EEG bands instead of
    a single generic passband -- this is the per-channel theta/alpha/beta/gamma
    decomposition used for data validation, one band per key in the result.

    Returns dict with 'bands' ({name: filtered array}), 'saturation_mask' and
    'excluded_mask' (same semantics as `apply_pipeline`).
    """
    stage = raw.astype(np.float64, copy=True)

    sat_params = params.get("saturation", {})
    if sat_params.get("enabled", True):
        threshold = float(sat_params.get("threshold", DEFAULT_SATURATION_UV))
        saturation_mask = detect_saturation(raw, threshold)
    else:
        saturation_mask = np.zeros_like(raw, dtype=bool)

    excluded_mask = np.zeros_like(saturation_mask)
    if sat_params.get("enabled", True) and sat_params.get("interpolate", True) and saturation_mask.any():
        max_gap_ms = float(sat_params.get("max_gap_ms", DEFAULT_MAX_GAP_MS))
        max_gap_samples = max(1, round(max_gap_ms / 1000.0 * fs))
        stage, excluded_mask = interpolate_saturation(stage, saturation_mask, max_gap_samples)

    dt_params = params.get("detrend", {"enabled": True, "kind": "constant"})
    if dt_params.get("enabled", True):
        stage = detrend(stage, dt_params.get("kind", "constant"))

    notch_params = params.get("notch", {"enabled": True, "freq": 60.0, "q": 30.0, "harmonics": 1})
    if notch_params.get("enabled", True):
        stage = notch(
            stage, fs,
            float(notch_params.get("freq", 60.0)),
            float(notch_params.get("q", 30.0)),
            int(notch_params.get("harmonics", 1)),
        )

    band_order = int(params.get("band_order", 4))
    bands = decompose_bands(stage, fs, order=band_order)

    return {"bands": bands, "saturation_mask": saturation_mask, "excluded_mask": excluded_mask}


def apply_pipeline(raw: np.ndarray, fs: float, params: dict) -> dict:
    """Run [interpolate saturation] -> detrend -> bandpass -> notch on raw (n_samples, n_channels).

    Returns dict with 'filtered' array, 'saturation_mask' and 'excluded_mask'
    (long saturated runs that were interpolated only to keep the filter stable,
    not because the signal was recovered) boolean arrays.
    """
    stage = raw.astype(np.float64, copy=True)

    sat_params = params.get("saturation", {})
    if sat_params.get("enabled", True):
        threshold = float(sat_params.get("threshold", DEFAULT_SATURATION_UV))
        saturation_mask = detect_saturation(raw, threshold)
    else:
        saturation_mask = np.zeros_like(raw, dtype=bool)

    excluded_mask = np.zeros_like(saturation_mask)
    if sat_params.get("enabled", True) and sat_params.get("interpolate", True) and saturation_mask.any():
        max_gap_ms = float(sat_params.get("max_gap_ms", DEFAULT_MAX_GAP_MS))
        max_gap_samples = max(1, round(max_gap_ms / 1000.0 * fs))
        stage, excluded_mask = interpolate_saturation(stage, saturation_mask, max_gap_samples)

    dt_params = params.get("detrend", {})
    if dt_params.get("enabled", False):
        stage = detrend(stage, dt_params.get("kind", "constant"))

    band_params = params.get("bandpass", {})
    if band_params.get("enabled", False):
        stage = bandpass(
            stage, fs,
            float(band_params.get("low", 1.0)),
            float(band_params.get("high", 40.0)),
            int(band_params.get("order", 4)),
        )

    notch_params = params.get("notch", {})
    if notch_params.get("enabled", False):
        stage = notch(
            stage, fs,
            float(notch_params.get("freq", 60.0)),
            float(notch_params.get("q", 30.0)),
            int(notch_params.get("harmonics", 1)),
        )

    return {"filtered": stage, "saturation_mask": saturation_mask, "excluded_mask": excluded_mask}
