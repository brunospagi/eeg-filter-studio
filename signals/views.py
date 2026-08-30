import csv
import functools
import io
import json

import numpy as np
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from . import dsp, parsing

MAX_PLOT_POINTS = 20000  # per channel, per trace -- keeps the browser responsive


def login_required_json(view_func):
    """Like @login_required, but for fetch()-called API endpoints: a 302
    redirect to the login page would just confuse postJSON/postFile (they'd
    try to parse the login HTML as JSON), so this returns a plain 401 the
    frontend can react to instead."""
    @functools.wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"ok": False, "error": "Sessão expirada. Faça login novamente."}, status=401)
        return view_func(request, *args, **kwargs)
    return wrapper


@login_required
def index(request):
    return render(request, "signals/index.html")


def _error(message: str, status: int = 400) -> JsonResponse:
    return JsonResponse({"ok": False, "error": message}, status=status)


def _decimate(arr: np.ndarray, max_points: int) -> np.ndarray:
    n = arr.shape[0]
    if n <= max_points:
        return arr
    stride = int(np.ceil(n / max_points))
    return arr[::stride]


@require_POST
@login_required_json
def api_upload(request):
    file_obj = request.FILES.get("file")
    if file_obj is None:
        return _error("Nenhum arquivo enviado.")
    try:
        parsed = parsing.parse_csv(file_obj)
    except parsing.ParseError as exc:
        return _error(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _error(f"Erro inesperado ao processar o CSV: {exc}", status=500)

    name = request.POST.get("name", "")
    is_eyes_test = request.POST.get("is_eyes_test") in ("1", "true", "True", "on")
    token = parsing.save_session(
        parsed,
        name=name,
        original_filename=file_obj.name,
        participant_name=request.POST.get("participant_name", ""),
        sex=request.POST.get("sex", ""),
        age=request.POST.get("age", ""),
        is_eyes_test=is_eyes_test,
    )
    n_samples, n_channels = parsed["eeg"].shape
    return JsonResponse({
        "ok": True,
        "token": token,
        "fs": parsed["fs"],
        "n_samples": n_samples,
        "n_channels": n_channels,
        "channel_names": parsed["channel_names"],
        "duration_s": float(parsed["timestamps"][-1]),
        "markers": parsed["markers"],
        "default_saturation_uv": dsp.DEFAULT_SATURATION_UV,
        "is_eyes_test": is_eyes_test,
    })


@require_GET
@login_required_json
def api_tests_list(request):
    return JsonResponse({"ok": True, "tests": parsing.list_tests()})


@require_POST
@login_required_json
def api_test_delete(request):
    try:
        body = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _error("Corpo da requisição não é um JSON válido.")
    token = body.get("token")
    if not token:
        return _error("Campo 'token' ausente.")
    try:
        parsing.delete_test(token)
    except parsing.ParseError as exc:
        return _error(str(exc))
    return JsonResponse({"ok": True})


@require_POST
@login_required_json
def api_artifacts_save(request):
    """Persist manually-marked artifact ranges (blinks, jaw clench, etc. --
    visually identified, per Anghinah et al., since they leave no trace the
    software can detect the way ADC saturation does) for a registered test."""
    try:
        body = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _error("Corpo da requisição não é um JSON válido.")
    token = body.get("token")
    if not token:
        return _error("Campo 'token' ausente.")
    ranges = body.get("ranges") or []
    try:
        parsing.save_artifacts(token, ranges)
    except parsing.ParseError as exc:
        return _error(str(exc))
    return JsonResponse({"ok": True, "count": len(ranges)})


def _parse_request_params(request):
    try:
        body = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise parsing.ParseError("Corpo da requisição não é um JSON válido.")
    token = body.get("token")
    if not token:
        raise parsing.ParseError("Campo 'token' ausente.")
    params = body.get("params", {})
    channels = body.get("channels")  # list of channel names, or None = all
    return token, params, channels


@require_POST
@login_required_json
def api_process(request):
    try:
        token, params, channels = _parse_request_params(request)
        session = parsing.load_session(token)
    except parsing.ParseError as exc:
        return _error(str(exc))

    channel_names = session["channel_names"]
    eeg = session["eeg"]
    fs = session["fs"]
    timestamps = session["timestamps"]

    if channels:
        idx = [channel_names.index(c) for c in channels if c in channel_names]
    else:
        idx = list(range(len(channel_names)))
    if not idx:
        return _error("Nenhum canal válido selecionado.")

    raw_sel = eeg[:, idx]
    try:
        result = dsp.apply_pipeline(raw_sel, fs, params, timestamps=timestamps,
                                     artifact_ranges=session["artifact_ranges"])
    except dsp.FilterError as exc:
        return _error(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _error(f"Erro ao aplicar filtros: {exc}", status=500)

    filtered = result["filtered"]
    sat_mask = result["saturation_mask"]
    excluded_mask = result["excluded_mask"]

    t_dec = _decimate(timestamps, MAX_PLOT_POINTS)
    stride = int(np.ceil(len(timestamps) / MAX_PLOT_POINTS)) if len(timestamps) > MAX_PLOT_POINTS else 1

    channels_out = {}
    for j, ch_idx in enumerate(idx):
        name = channel_names[ch_idx]
        raw_dec = raw_sel[::stride, j] if stride > 1 else raw_sel[:, j]
        filt_dec = filtered[::stride, j] if stride > 1 else filtered[:, j]
        sat_indices = np.nonzero(sat_mask[:, j])[0]
        # cap markers sent to the client so a fully-railed channel doesn't blow up the payload
        if sat_indices.size > 2000:
            sat_indices = sat_indices[:: int(np.ceil(sat_indices.size / 2000))]
        excluded_ranges = dsp.mask_to_time_ranges(excluded_mask[:, j], timestamps)
        channels_out[name] = {
            "raw": np.round(raw_dec, 2).tolist(),
            "filtered": np.round(filt_dec, 2).tolist(),
            "saturation_t": np.round(timestamps[sat_indices], 3).tolist(),
            "saturation_pct": round(100.0 * sat_mask[:, j].mean(), 2),
            "excluded_ranges": excluded_ranges[:500],  # cap payload for pathological cases
            "excluded_pct": round(100.0 * excluded_mask[:, j].mean(), 2),
        }

    return JsonResponse({
        "ok": True,
        "t": np.round(t_dec, 3).tolist(),
        "channels": channels_out,
        "artifact_ranges": session["artifact_ranges"],
        "decimated": stride > 1,
        "stride": stride,
    })


@require_POST
@login_required_json
def api_bands(request):
    """Per-channel decomposition into the canonical EEG bands (theta/alpha/
    beta/gamma) -- used by the "Bandas" view to validate that each channel
    carries a physiologically plausible spectral profile, not just noise."""
    try:
        token, params, channels = _parse_request_params(request)
        session = parsing.load_session(token)
    except parsing.ParseError as exc:
        return _error(str(exc))

    channel_names = session["channel_names"]
    eeg = session["eeg"]
    fs = session["fs"]
    timestamps = session["timestamps"]

    if channels:
        idx = [channel_names.index(c) for c in channels if c in channel_names]
    else:
        idx = list(range(len(channel_names)))
    if not idx:
        return _error("Nenhum canal válido selecionado.")

    raw_sel = eeg[:, idx]
    try:
        result = dsp.apply_band_pipeline(raw_sel, fs, params, timestamps=timestamps,
                                          artifact_ranges=session["artifact_ranges"])
    except dsp.FilterError as exc:
        return _error(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _error(f"Erro ao separar bandas: {exc}", status=500)

    bands = result["bands"]
    band_names = list(bands.keys())
    sat_mask = result["saturation_mask"]
    invalid_mask = result["invalid_mask"]

    t_dec = _decimate(timestamps, MAX_PLOT_POINTS)
    stride = int(np.ceil(len(timestamps) / MAX_PLOT_POINTS)) if len(timestamps) > MAX_PLOT_POINTS else 1

    channels_out = {}
    for j, ch_idx in enumerate(idx):
        name = channel_names[ch_idx]
        band_series = {}
        band_power = {}
        for band_name in band_names:
            arr = bands[band_name][:, j]
            band_series[band_name] = np.round(arr[::stride] if stride > 1 else arr, 2).tolist()
            # RMS, uV -- excludes the filter edge margin, saturated/interpolated
            # samples, and any manually-marked artifact range (see dsp.band_power_rms).
            band_power[band_name] = round(dsp.band_power_rms(arr, fs, exclude_mask=invalid_mask[:, j]), 2)
        channels_out[name] = {
            "bands": band_series,
            "band_power_rms_uv": band_power,
            "saturation_pct": round(100.0 * sat_mask[:, j].mean(), 2),
        }

    return JsonResponse({
        "ok": True,
        "t": np.round(t_dec, 3).tolist(),
        "band_names": band_names,
        "band_ranges": {k: list(v) for k, v in dsp.EEG_BANDS.items() if k in band_names},
        "channels": channels_out,
        "artifact_ranges": session["artifact_ranges"],
        "decimated": stride > 1,
        "stride": stride,
    })


@require_POST
@login_required_json
def api_bands_compare(request):
    """Per-channel, per-band power comparison between the eyes-closed and
    eyes-open segments of a single recording (lab protocol: 5 markers
    bounding closed1/open1/closed2/open2) -- lets a student validate one
    recording's Berger-effect-style pattern without leaving the app."""
    try:
        token, params, channels = _parse_request_params(request)
        session = parsing.load_session(token)
    except parsing.ParseError as exc:
        return _error(str(exc))

    channel_names = session["channel_names"]
    eeg = session["eeg"]
    fs = session["fs"]
    timestamps = session["timestamps"]
    marker_times = [m["sample_time"] for m in session["markers"]]

    if channels:
        idx = [channel_names.index(c) for c in channels if c in channel_names]
    else:
        idx = list(range(len(channel_names)))
    if not idx:
        return _error("Nenhum canal válido selecionado.")

    if not session.get("is_eyes_test"):
        return JsonResponse({
            "ok": True,
            "available": False,
            "reason": (
                "Esta gravação não está marcada como teste de olhos fechados/abertos. "
                "Marque essa opção no envio do CSV para habilitar esta comparação."
            ),
        })
    if len(marker_times) < 5:
        return JsonResponse({
            "ok": True,
            "available": False,
            "reason": (
                f"São necessários ao menos 5 marcadores (fechado/aberto/fechado/aberto) "
                f"para comparar; esta gravação tem {len(marker_times)}."
            ),
        })

    raw_sel = eeg[:, idx]
    try:
        result = dsp.apply_band_pipeline(raw_sel, fs, params, timestamps=timestamps,
                                          artifact_ranges=session["artifact_ranges"])
    except dsp.FilterError as exc:
        return _error(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _error(f"Erro ao comparar bandas: {exc}", status=500)

    comparison = dsp.band_power_by_markers(result["bands"], timestamps, marker_times, fs,
                                            invalid_mask=result["invalid_mask"])
    band_names = list(result["bands"].keys())
    names = [channel_names[i] for i in idx]

    channels_out = {}
    for j, name in enumerate(names):
        channels_out[name] = {
            band: {
                "closed_uv": round(float(comparison[band]["closed"][j]), 2),
                "open_uv": round(float(comparison[band]["open"][j]), 2),
            }
            for band in band_names
        }

    return JsonResponse({
        "ok": True,
        "available": True,
        "band_names": band_names,
        "band_ranges": {k: list(v) for k, v in dsp.EEG_BANDS.items() if k in band_names},
        "channels": channels_out,
    })


@require_POST
@login_required_json
def api_bands_download(request):
    try:
        token, params, channels = _parse_request_params(request)
        session = parsing.load_session(token)
    except parsing.ParseError as exc:
        return _error(str(exc))

    channel_names = session["channel_names"]
    eeg = session["eeg"]
    fs = session["fs"]
    timestamps = session["timestamps"]

    if channels:
        idx = [channel_names.index(c) for c in channels if c in channel_names]
    else:
        idx = list(range(len(channel_names)))
    if not idx:
        return _error("Nenhum canal válido selecionado.")

    raw_sel = eeg[:, idx]
    try:
        result = dsp.apply_band_pipeline(raw_sel, fs, params)
    except dsp.FilterError as exc:
        return _error(str(exc))

    bands = result["bands"]
    band_names = list(bands.keys())
    names = [channel_names[i] for i in idx]

    buf = io.StringIO()
    writer = csv.writer(buf)
    header = ["Time (s)"]
    for n in names:
        for band_name in band_names:
            header.append(f"{n}_{band_name}")
    writer.writerow(header)
    for row_i in range(len(timestamps)):
        row = [f"{timestamps[row_i]:.4f}"]
        for j in range(len(idx)):
            for band_name in band_names:
                row.append(f"{bands[band_name][row_i, j]:.3f}")
        writer.writerow(row)

    response = HttpResponse(buf.getvalue(), content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="eeg_bandas.csv"'
    return response


@require_POST
@login_required_json
def api_compare(request):
    """Compare band power and filtered signal across two or more registered
    tests -- e.g. two different participants, or two sessions of the same
    participant -- instead of only within one recording (closed vs open)."""
    try:
        body = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _error("Corpo da requisição não é um JSON válido.")
    tokens = body.get("tokens") or []
    if len(tokens) < 2:
        return _error("Selecione ao menos 2 testes para comparar.")
    params = body.get("params", {})
    requested_channels = body.get("channels")

    sessions = {}
    for token in tokens:
        try:
            sessions[token] = parsing.load_session(token)
        except parsing.ParseError as exc:
            return _error(f"Teste {token[:8]}…: {exc}")

    common = None
    for session in sessions.values():
        names = set(session["channel_names"])
        common = names if common is None else (common & names)
    if requested_channels:
        common = common & set(requested_channels)
    if not common:
        return _error("Nenhum canal em comum entre as gravações selecionadas.")
    channel_order = sessions[tokens[0]]["channel_names"]
    channels = [c for c in channel_order if c in common]

    tests_meta = {t["token"]: t for t in parsing.list_tests()}
    tests_out = [{"token": tok, "name": tests_meta.get(tok, {}).get("name", tok[:8])} for tok in tokens]

    band_power = {}
    series = {}
    markers = {}
    artifacts_by_token = {}
    band_names = None
    band_ranges = None

    for token in tokens:
        session = sessions[token]
        markers[token] = session["markers"]
        artifacts_by_token[token] = session["artifact_ranges"]
        channel_names = session["channel_names"]
        idx = [channel_names.index(c) for c in channels]
        fs = session["fs"]
        timestamps = session["timestamps"]
        raw_sel = session["eeg"][:, idx]

        artifact_ranges = session["artifact_ranges"]
        try:
            band_result = dsp.apply_band_pipeline(raw_sel, fs, params, timestamps=timestamps,
                                                    artifact_ranges=artifact_ranges)
            filt_result = dsp.apply_pipeline(raw_sel, fs, params, timestamps=timestamps,
                                              artifact_ranges=artifact_ranges)
        except dsp.FilterError as exc:
            return _error(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _error(f"Erro ao comparar '{tests_meta.get(token, {}).get('name', token)}': {exc}", status=500)

        bands = band_result["bands"]
        invalid_mask = band_result["invalid_mask"]
        if band_names is None:
            band_names = list(bands.keys())
            band_ranges = {k: list(v) for k, v in dsp.EEG_BANDS.items() if k in band_names}

        band_power[token] = {
            name: {
                band: round(dsp.band_power_rms(bands[band][:, j], fs, exclude_mask=invalid_mask[:, j]), 2)
                for band in band_names
            }
            for j, name in enumerate(channels)
        }

        t_dec = _decimate(timestamps, MAX_PLOT_POINTS)
        stride = int(np.ceil(len(timestamps) / MAX_PLOT_POINTS)) if len(timestamps) > MAX_PLOT_POINTS else 1
        filtered = filt_result["filtered"]
        series[token] = {
            "t": np.round(t_dec, 3).tolist(),
            "channels": {
                name: np.round(filtered[::stride, j] if stride > 1 else filtered[:, j], 2).tolist()
                for j, name in enumerate(channels)
            },
        }

    return JsonResponse({
        "ok": True,
        "tests": tests_out,
        "band_names": band_names,
        "band_ranges": band_ranges,
        "channel_names": channels,
        "band_power": band_power,
        "series": series,
        "markers": markers,
        "artifact_ranges": artifacts_by_token,
    })


@require_POST
@login_required_json
def api_download(request):
    try:
        token, params, channels = _parse_request_params(request)
        session = parsing.load_session(token)
    except parsing.ParseError as exc:
        return _error(str(exc))

    channel_names = session["channel_names"]
    eeg = session["eeg"]
    fs = session["fs"]
    timestamps = session["timestamps"]

    if channels:
        idx = [channel_names.index(c) for c in channels if c in channel_names]
    else:
        idx = list(range(len(channel_names)))
    if not idx:
        return _error("Nenhum canal válido selecionado.")

    raw_sel = eeg[:, idx]
    try:
        result = dsp.apply_pipeline(raw_sel, fs, params, timestamps=timestamps,
                                     artifact_ranges=session["artifact_ranges"])
    except dsp.FilterError as exc:
        return _error(str(exc))

    filtered = result["filtered"]
    sat_mask = result["saturation_mask"]
    excluded_mask = result["excluded_mask"]
    artifact_mask = result["artifact_mask"]
    names = [channel_names[i] for i in idx]

    buf = io.StringIO()
    writer = csv.writer(buf)
    header = ["Time (s)"]
    for n in names:
        header += [f"{n}_raw", f"{n}_filtered", f"{n}_saturated", f"{n}_excluded", f"{n}_artifact"]
    writer.writerow(header)
    for row_i in range(len(timestamps)):
        row = [f"{timestamps[row_i]:.4f}"]
        for j in range(len(idx)):
            row += [
                f"{raw_sel[row_i, j]:.3f}",
                f"{filtered[row_i, j]:.3f}",
                int(sat_mask[row_i, j]),
                int(excluded_mask[row_i, j]),
                int(artifact_mask[row_i]),
            ]
        writer.writerow(row)

    response = HttpResponse(buf.getvalue(), content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="eeg_filtered.csv"'
    return response
