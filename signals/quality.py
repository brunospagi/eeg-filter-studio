"""Verificações automáticas de qualidade para gravações OpenBCI Cyton.

As verificações rodam sobre o sinal BRUTO (µV, amostras x canais) e respondem a
uma pergunta anterior a qualquer métrica: o registro tem qualidade suficiente
para que RMS, bandas ou comparações entre condições signifiquem algo?

1. Interferência de rede  -> fração da potência e amplitude em torno de 60 Hz.
2. Saturação              -> amostras no fundo de escala do ADS1299; canais mortos.
3. Canais idênticos       -> pares cuja diferença é menor que o próprio EEG (< 20 µV).
4. Continuidade           -> saltos de fase da portadora de rede (amostra perdida
                             ou duplicada no caminho placa -> PC -> software).
5. Transiente de borda    -> RMS do 1º segundo filtrado vs. o restante.

Uso:
    from signals.quality import check_recording
    rep = check_recording(raw, fs=250)
    rep.ok, rep.flags, rep.as_dict()

Só depende de NumPy/SciPy; os limiares padrão estão em DEFAULT_CRITERIA.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

import numpy as np
from scipy import signal

from .dsp import DEFAULT_SATURATION_UV as FULL_SCALE_UV, bandpass, detrend

DEFAULT_CRITERIA = {
    "mains_freq": 60.0,          # Hz
    "mains_max_uv": 100.0,       # amplitude de pico máxima aceitável da rede no sinal bruto
    "saturation_max_frac": 0.0,  # qualquer amostra no fundo de escala reprova
    "identical_max_diff_uv": 20.0,  # DP da diferença entre canais abaixo disso = idênticos
    "slip_step_rad": 0.8,        # salto de fase mínimo (1 amostra a 60 Hz/250 Hz = 1,51 rad)
    "slip_min_mains_uv": 200.0,  # abaixo disso a portadora é fraca demais para detectar saltos
    "edge_max_ratio": 3.0,       # RMS(0–1 s)/RMS(2–10 s) acima disso = transiente de borda
    "edge_band": (1.0, 40.0),
    "edge_order": 4,
}


@dataclass
class QualityReport:
    fs: float
    n_samples: int
    n_channels: int
    mains_share: list = field(default_factory=list)       # fração da potência em ±1 Hz da rede, por canal
    mains_amplitude_uv: list = field(default_factory=list)
    saturation_frac: list = field(default_factory=list)
    dead_channels: list = field(default_factory=list)     # 1-based
    identical_pairs: list = field(default_factory=list)   # [(i, j, dp_diferenca_uv)] 1-based
    slip_times_s: list = field(default_factory=list)
    slip_channel: int | None = None                       # 1-based; None = não avaliável
    duplicated_rows: int = 0
    edge_ratio: float | None = None
    flags: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.flags

    def as_dict(self) -> dict:
        d = asdict(self)
        d["ok"] = self.ok
        return d


# ---------------------------------------------------------------- verificações

def mains_interference(raw: np.ndarray, fs: float, freq: float = 60.0, half_band: float = 1.0):
    """Fração da potência (Welch, >0,5 Hz) entre freq±half_band e amplitude de pico estimada (µV)."""
    x = raw - np.nanmean(raw, axis=0)
    nper = int(min(len(x), 4 * fs))
    f, p = signal.welch(np.nan_to_num(x), fs, nperseg=nper, axis=0)
    df = f[1] - f[0]
    band = (f >= freq - half_band) & (f <= freq + half_band)
    total = p[f > 0.5].sum(axis=0)
    share = np.divide(p[band].sum(axis=0), total, out=np.zeros(p.shape[1]), where=total > 0)
    amp = np.sqrt(2 * p[band].sum(axis=0) * df)
    return share, amp


def saturation(raw: np.ndarray, threshold: float = FULL_SCALE_UV):
    """Fração de amostras com |x| >= limiar por canal e lista de canais mortos (saturados >99,9 %)."""
    m = np.abs(raw) >= threshold
    frac = m.mean(axis=0)
    dead = [int(c) + 1 for c in np.where(frac > 0.999)[0]]
    return frac, dead


def identical_channels(raw: np.ndarray, max_diff_uv: float = 20.0):
    """Pares de canais praticamente idênticos (1-based): desvio-padrão da diferença bruta < max_diff_uv.

    Usa a diferença, e não a correlação, porque uma interferência de rede comum de
    dezenas de mV leva a r > 0,999 entre canais que de fato diferem.
    """
    out = []
    for i in range(raw.shape[1]):
        for j in range(i + 1, raw.shape[1]):
            d = raw[:, i] - raw[:, j]
            if not np.isfinite(d).any() or np.nanstd(raw[:, i]) == 0:
                continue
            sd = float(np.nanstd(d))
            if sd < max_diff_uv:
                out.append((i + 1, j + 1, round(sd, 2)))
    return out


def _mains_freq(x: np.ndarray, fs: float, nominal: float) -> float:
    n = len(x)
    spec = np.abs(np.fft.rfft(x * np.hanning(n), n=8 * n))
    f = np.fft.rfftfreq(8 * n, 1 / fs)
    sel = (f > nominal - 1) & (f < nominal + 1)
    return float(f[sel][np.argmax(spec[sel])])


def sample_slips(x: np.ndarray, fs: float, nominal: float = 60.0, step_rad: float = 0.8,
                 guard_s: float = 0.04, win_s: float = 0.76, min_sep_s: float = 1.0):
    """Instantes (s) em que a fase da portadora de rede salta, indicando amostra perdida/duplicada.

    Demodula o canal em torno da frequência da rede, mede a fase média em janelas
    antes/depois de cada instante e marca saltos >= step_rad. Requer portadora forte.
    """
    x = np.asarray(x, float) - np.mean(x)
    n = len(x)
    t = np.arange(n) / fs
    f0 = _mains_freq(x, fs, nominal)
    z = x * np.exp(-2j * np.pi * f0 * t)
    k = max(int(0.2 * fs), 1)
    z = np.convolve(z, np.ones(k) / k, mode="same")
    ph = np.unwrap(np.angle(z))
    slope = np.median(np.diff(ph))                    # resíduo de frequência (robusto a degraus)
    ph = ph - slope * np.arange(n)
    g, w = int(guard_s * fs), int(win_s * fs)
    c = np.concatenate([[0.0], np.cumsum(ph)])
    idx = np.arange(g + w + k, n - g - w - k)
    after = (c[idx + g + w] - c[idx + g]) / w
    before = (c[idx - g] - c[idx - g - w]) / w
    step = after - before
    cand = np.where(np.abs(step) >= step_rad)[0]
    times, last = [], -np.inf
    for i in cand[np.argsort(-np.abs(step[cand]))]:  # picos mais fortes primeiro
        ti = t[idx[i]]
        if all(abs(ti - s) >= min_sep_s for s in times):
            times.append(ti)
    return sorted(round(float(s), 2) for s in times)


def edge_transient(raw: np.ndarray, fs: float, low: float = 1.0, high: float = 40.0, order: int = 4):
    """RMS do 1º segundo / RMS de 2–10 s após remoção de DC e passa-banda da aplicação (mediana entre canais)."""
    if len(raw) < int(10 * fs):
        return None
    x = bandpass(detrend(np.nan_to_num(raw), "constant"), fs, low, high, order)
    first = np.sqrt(np.mean(x[: int(fs)] ** 2, axis=0))
    rest = np.sqrt(np.mean(x[int(2 * fs): int(10 * fs)] ** 2, axis=0))
    ok = rest > 0
    return float(np.median(first[ok] / rest[ok])) if ok.any() else None


# ---------------------------------------------------------------- agregador

def check_recording(raw: np.ndarray, fs: float = 250.0, criteria: dict | None = None) -> QualityReport:
    """Roda as cinco verificações e devolve um relatório com os critérios reprovados em `flags`."""
    c = {**DEFAULT_CRITERIA, **(criteria or {})}
    raw = np.asarray(raw, float)
    rep = QualityReport(fs=fs, n_samples=raw.shape[0], n_channels=raw.shape[1])

    if not np.isfinite(raw).any():
        rep.flags.append("sem dados de EEG (todos os valores ausentes)")
        return rep

    share, amp = mains_interference(raw, fs, c["mains_freq"])
    rep.mains_share = [round(float(v), 4) for v in share]
    rep.mains_amplitude_uv = [round(float(v), 1) for v in amp]

    frac, dead = saturation(raw)
    rep.saturation_frac = [round(float(v), 5) for v in frac]
    rep.dead_channels = dead
    rep.identical_pairs = identical_channels(raw, c["identical_max_diff_uv"])
    rep.duplicated_rows = int((np.abs(np.diff(raw, axis=0)).sum(axis=1) == 0).sum())

    usable = (frac < 0.01) & (amp >= c["slip_min_mains_uv"])
    if usable.any():
        ch = int(np.argmax(np.where(usable, amp, -1)))
        rep.slip_channel = ch + 1
        rep.slip_times_s = sample_slips(raw[:, ch], fs, c["mains_freq"], c["slip_step_rad"])

    rep.edge_ratio = edge_transient(raw, fs, *c["edge_band"], c["edge_order"])

    # critérios
    live = [i for i in range(raw.shape[1]) if (i + 1) not in dead]
    if live and np.median(amp[live]) > c["mains_max_uv"]:
        rep.flags.append(f"interferência de rede alta: {np.median(amp[live]) / 1000:.1f} mV de pico (mediana)")
    if frac.max() > c["saturation_max_frac"]:
        rep.flags.append(f"saturação: {100 * frac.mean():.2f} % das amostras no fundo de escala")
    if dead:
        rep.flags.append(f"canal(is) morto(s): {dead}")
    if rep.identical_pairs:
        rep.flags.append(f"canais idênticos: {[(i, j) for i, j, _ in rep.identical_pairs]}")
    if rep.slip_times_s:
        rep.flags.append(f"saltos de amostra em {rep.slip_times_s} s")
    if rep.duplicated_rows:
        rep.flags.append(f"{rep.duplicated_rows} linha(s) duplicada(s) consecutiva(s)")
    if rep.edge_ratio is not None and rep.edge_ratio > c["edge_max_ratio"]:
        rep.flags.append(f"transiente de borda: 1º segundo {rep.edge_ratio:.1f}x o restante — descartar as bordas")
    return rep


# ---------------------------------------------------------------- comparativo entre gravações

def load_openbci_csv(path, until_marker: int | None = None):
    """Lê um CSV do OpenBCI/EventIDE sem descartar linhas (preserva NaN e duplicações).

    Devolve (raw µV amostras x canais, fs, lista de índices dos marcadores). Com
    `until_marker=k`, corta o sinal no k-ésimo marcador (ex.: 5 = fim do 4º bloco).
    """
    import pandas as pd

    df = pd.read_csv(path, skip_blank_lines=True)
    df.columns = [c.strip() for c in df.columns]
    chans = sorted((c for c in df.columns if c.lower().startswith("eeg channel")),
                   key=lambda c: int(c.split()[-1]))
    raw = df[chans].apply(pd.to_numeric, errors="coerce").to_numpy(float)
    ts = pd.to_numeric(df[next(c for c in df.columns if c.lower() == "timestamp")], errors="coerce").to_numpy(float)
    d = np.diff(ts[np.isfinite(ts)])
    d = d[d > 0]
    fs = float(round(1000.0 / np.median(d))) if len(d) else 250.0   # Timestamp em ms
    markers = []
    tag = next((c for c in df.columns if c.lower() == "marker tag"), None)
    if tag is not None:
        m = df[tag]
        markers = list(np.where((m.notna() & (m.astype(str).str.strip() != "")).to_numpy())[0])
    if until_marker and len(markers) >= until_marker:
        raw = raw[: markers[until_marker - 1]]
    return raw, fs, markers


def summary_row(label: str, rep: QualityReport) -> dict:
    """Uma linha do comparativo entre gravações."""
    live = [a for i, a in enumerate(rep.mains_amplitude_uv) if (i + 1) not in rep.dead_channels]
    ch_ident = sorted({c for i, j, _ in rep.identical_pairs for c in (i, j)})
    return {
        "gravacao": label,
        "ok": rep.ok,
        "rede_mV": round(float(np.median(live)) / 1000, 1) if live else None,
        "rede_pct_potencia": round(100 * float(np.median(rep.mains_share)), 1) if rep.mains_share else None,
        "saturacao_pct": round(100 * float(np.mean(rep.saturation_frac)), 2) if rep.saturation_frac else None,
        "canais_mortos": " ".join(map(str, rep.dead_channels)),
        "canais_identicos": " ".join(map(str, ch_ident)),
        "saltos": len(rep.slip_times_s),
        "saltos_s": " ".join(f"{s:g}" for s in rep.slip_times_s),
        "linhas_duplicadas": rep.duplicated_rows,
        "borda_razao": round(rep.edge_ratio, 1) if rep.edge_ratio is not None else None,
        "n_problemas": len(rep.flags),
        "problemas": "; ".join(rep.flags),
    }


def compare_recordings(paths, labels=None, until_marker: int | None = None, criteria: dict | None = None) -> list[dict]:
    """Roda check_recording em várias gravações e devolve o comparativo (uma linha por arquivo)."""
    rows = []
    for k, p in enumerate(paths):
        raw, fs, _ = load_openbci_csv(p, until_marker)
        rep = check_recording(raw, fs, criteria)
        rows.append(summary_row(labels[k] if labels else str(p), rep))
    return rows
