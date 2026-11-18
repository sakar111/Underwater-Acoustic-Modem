"""Measurements and display helpers for RX data (spec §6.5, §6.6). Pure NumPy, no Qt.

Signals are ADC codes (uint16, 12-bit) or AC volts at the ADC ((code - mid) * LSB).
"""
from __future__ import annotations

import math

import numpy as np

from .rx_model import FS_HZ, LSB_V, RxGain

TX_RAMP_US = 100.0                # TX amplitude ramp at each burst edge (TX spec §4.3)


# ---------------------------------------------------------------- basic levels
def adc_vpp(codes) -> float:
    c = np.asarray(codes)
    return float(c.max() - c.min()) * LSB_V if c.size else 0.0


def adc_vrms(codes) -> float:
    c = np.asarray(codes, dtype=float)
    return float(c.std()) * LSB_V if c.size else 0.0


def ac_volts(codes, mid: float) -> np.ndarray:
    return (np.asarray(codes, dtype=float) - mid) * LSB_V


# ---------------------------------------------------------------- spectra
def spectrum(x, fs: float = FS_HZ, nfft: int = 8192) -> tuple[np.ndarray, np.ndarray]:
    """Hann-windowed amplitude spectrum. Returns (freqs, sine-peak amplitude in the units of x).

    x shorter than nfft is zero-padded (the window covers the data only); longer x uses the
    last nfft samples.
    """
    x = np.asarray(x, dtype=float)
    if x.size > nfft:
        x = x[-nfft:]
    x = x - x.mean() if x.size else x
    w = np.hanning(x.size) if x.size > 1 else np.ones(max(x.size, 1))
    xs = np.zeros(nfft)
    xs[:x.size] = x * w[:x.size]
    mag = np.abs(np.fft.rfft(xs)) * 2 / max(w.sum(), 1e-12)
    return np.fft.rfftfreq(nfft, 1 / fs), mag


def to_db(a, floor: float = 1e-9) -> np.ndarray:
    return 20 * np.log10(np.maximum(np.asarray(a, dtype=float), floor))


def peak_frequency(x, fs: float = FS_HZ, nfft: int = 8192, fmin: float = 1e3,
                   fmax: float | None = None) -> tuple[float, float]:
    """FFT peak (Hann window) with parabolic interpolation on the log magnitude.

    Returns (frequency in Hz, sine-peak amplitude in the units of x).
    """
    x = np.asarray(x, dtype=float)
    if x.size < 8:
        return 0.0, 0.0
    n = max(nfft, 1 << int(math.ceil(math.log2(min(x.size, 1 << 20)))))
    f, mag = spectrum(x[-n:], fs, n)
    lo = int(np.searchsorted(f, fmin))
    hi = len(f) - 1 if fmax is None else int(np.searchsorted(f, fmax))
    if hi - lo < 3:
        return 0.0, 0.0
    k = lo + int(np.argmax(mag[lo:hi]))
    if 0 < k < len(mag) - 1 and mag[k] > 0:
        a, b, c = np.log(np.maximum(mag[k - 1:k + 2], 1e-30))
        den = a - 2 * b + c
        p = 0.5 * (a - c) / den if den != 0 else 0.0
        amp = math.exp(b - 0.25 * (a - c) * p)
    else:
        p, amp = 0.0, float(mag[k])
    return float((k + p) * fs / n), float(amp)


def spectrogram(x, nfft: int = 1024, hop: int = 512) -> np.ndarray:
    """Columns of Hann-windowed |FFT| (sine-peak scaling), shape (n_columns, nfft // 2 + 1)."""
    x = np.asarray(x, dtype=float)
    if x.size < nfft:
        return np.zeros((0, nfft // 2 + 1))
    ncol = 1 + (x.size - nfft) // hop
    idx = np.arange(nfft)[None, :] + hop * np.arange(ncol)[:, None]
    w = np.hanning(nfft)
    seg = x[idx]
    seg = seg - seg.mean(axis=1, keepdims=True)
    return np.abs(np.fft.rfft(seg * w, axis=1)) * 2 / w.sum()


def minmax_decimate(y, max_points: int = 4000) -> tuple[np.ndarray, np.ndarray]:
    """Min/max envelope reduction for plotting (never aliases). Returns (sample index, value)."""
    y = np.asarray(y)
    n = y.size
    if n <= max_points:
        return np.arange(n, dtype=float), y.astype(float)
    bins = max_points // 2
    step = int(math.ceil(n / bins))
    m = n // step
    blk = y[:m * step].reshape(m, step)
    out = np.empty(2 * m)
    out[0::2] = blk.min(axis=1)
    out[1::2] = blk.max(axis=1)
    idx = np.repeat(np.arange(m) * step + step / 2, 2).astype(float)
    if n > m * step:
        tail = y[m * step:]
        out = np.concatenate((out, [tail.min(), tail.max()]))
        idx = np.concatenate((idx, [m * step + tail.size / 2] * 2))
    return idx, out


# ---------------------------------------------------------------- delay (§6.6)
def burst_template(freq_hz: float, on_us: float, fs: float = FS_HZ, ramp_us: float = TX_RAMP_US) -> np.ndarray:
    """Tone burst as the TX board sends it: phase 0 at the sync edge, linear 100 us amplitude ramps."""
    n = max(int(round(on_us * 1e-6 * fs)), 1)
    nr = int(round(ramp_us * 1e-6 * fs))
    t = np.arange(n + nr) / fs
    env = np.ones(n + nr)
    if nr > 0:
        env[:nr] = np.arange(1, nr + 1) / nr                    # ramp up
        env[n:] = 1 - np.arange(1, nr + 1) / nr                 # ramp down after the on-time
    return env * np.sin(2 * np.pi * freq_hz * t)


def delay_threshold(x_ac, trig: int, peak: float | None = None) -> int | None:
    """First sample at/after `trig` with |x| >= max(6 x pre-trigger rms, 20 % of the peak).

    Returns the delay in samples relative to the trigger, or None.
    """
    x = np.asarray(x_ac, dtype=float)
    if trig >= x.size:
        return None
    pre = x[:trig]
    noise = float(np.sqrt(np.mean(pre ** 2))) if pre.size >= 8 else 0.0
    post = np.abs(x[trig:])
    if peak is None:
        peak = float(post.max()) if post.size else 0.0
    thr = max(6 * noise, 0.2 * peak)
    if thr <= 0:
        return None
    hit = np.flatnonzero(post >= thr)
    return int(hit[0]) if hit.size else None


def _analytic_envelope(x: np.ndarray) -> np.ndarray:
    n = x.size
    X = np.fft.fft(x)
    h = np.zeros(n)
    h[0] = 1
    if n % 2 == 0:
        h[n // 2] = 1
        h[1:n // 2] = 2
    else:
        h[1:(n + 1) // 2] = 2
    return np.abs(np.fft.ifft(X * h))


def delay_xcorr(x_ac, trig: int, template, carrier_hz: float | None = None, fs: float = FS_HZ) -> int | None:
    """Lag (samples after the trigger) where the template best matches.

    The envelope of the cross-correlation finds the burst; its top is flat for a ramped burst, so
    the lag is refined to the largest correlation (carrier) peak within half a carrier period.
    """
    x = np.asarray(x_ac, dtype=float)[trig:]
    t = np.asarray(template, dtype=float)
    if x.size < t.size or t.size < 2:
        return None
    n = 1 << int(math.ceil(math.log2(x.size + t.size)))
    c = np.fft.irfft(np.fft.rfft(x, n) * np.conj(np.fft.rfft(t, n)), n)[:x.size - t.size + 1]
    env = _analytic_envelope(c)
    k = int(np.argmax(env))
    if carrier_hz is None:
        f, _ = peak_frequency(t, fs, nfft=max(8192, 1 << int(math.ceil(math.log2(t.size)))))
        carrier_hz = f
    half = int(round(fs / carrier_hz / 2)) if carrier_hz and carrier_hz > 0 else 0
    if half > 0:
        lo, hi = max(0, k - half), min(c.size, k + half + 1)
        k = lo + int(np.argmax(c[lo:hi]))
    return k


# ---------------------------------------------------------------- measurement table
def measure(codes, mid: float, gain: RxGain, *, trig: int | None = None, win: tuple[int, int] | None = None,
            fs: float = FS_HZ, rvs_db: float | None = None, template=None, sync_ok: bool = False,
            system_delay_us: float = 0.0, sound_speed: float = 1480.0, f_hint: float | None = None) -> dict:
    """The §6.6 measurement set on `codes[win[0]:win[1]]` (default: trigger -> end, else all).

    Delay and distance need a sync-triggered capture (`sync_ok`): delays are relative to `trig`.
    """
    codes = np.asarray(codes)
    n = codes.size
    if win is None:
        win = (trig if trig is not None else 0, n)
    a, b = max(0, int(win[0])), min(n, int(win[1]))
    seg = codes[a:b]
    res: dict = {}
    if seg.size < 4:
        return res
    vpp = adc_vpp(seg)
    res["adc_vpp_v"] = vpp
    res["adc_vrms_v"] = adc_vrms(seg)
    f_peak, amp = peak_frequency(ac_volts(seg, mid), fs)
    res["freq_hz"] = f_peak
    f_use = f_hint or f_peak or 40e3
    g = gain.total_mag(f_use) if f_use > 0 else float("nan")
    res["gain_db_at_f"] = 20 * math.log10(g) if g > 0 else float("nan")
    res["input_vpp_v"] = vpp / g if g > 0 else float("nan")
    vin_rms = res["adc_vrms_v"] / g if g > 0 else float("nan")
    res["input_vrms_v"] = vin_rms
    if rvs_db is not None and vin_rms > 0:
        # SPL in dB re 1 uPa = 20 log10(V_rms / 1 V) - RVS (dB re 1 V/uPa)
        res["spl_db_re_1upa"] = 20 * math.log10(vin_rms) - rvs_db
    if trig is not None and trig >= 8:
        pre = codes[:trig].astype(float)
        noise = pre.std() * LSB_V
        res["noise_vrms_v"] = noise
        if noise > 0:
            res["snr_db"] = 20 * math.log10(max(res["adc_vrms_v"], 1e-12) / noise)
    if trig is not None and sync_ok:
        x = ac_volts(codes, mid)
        d_thr = delay_threshold(x, trig)
        if d_thr is not None:
            res["delay_thr_samples"] = d_thr
            res["delay_thr_us"] = d_thr * 1e6 / fs
        if template is not None:
            d_xc = delay_xcorr(x, trig, template)
            if d_xc is not None:
                res["delay_xc_samples"] = d_xc
                res["delay_xc_us"] = d_xc * 1e6 / fs
        d_us = res.get("delay_xc_us", res.get("delay_thr_us"))
        if d_us is not None:
            res["distance_m"] = sound_speed * (d_us - system_delay_us) * 1e-6
    return res


MEAS_LABELS = [
    ("adc_vpp_v", "ADC Vpp", lambda v: f"{v * 1e3:.1f} mV"),
    ("adc_vrms_v", "ADC Vrms", lambda v: f"{v * 1e3:.2f} mV"),
    ("freq_hz", "Frequency", lambda v: f"{v / 1e3:.3f} kHz"),
    ("gain_db_at_f", "Total gain @ f", lambda v: f"{v:.1f} dB"),
    ("input_vpp_v", "Input Vpp", lambda v: f"{v * 1e3:.4g} mV"),
    ("spl_db_re_1upa", "SPL", lambda v: f"{v:.1f} dB re 1 µPa"),
    ("snr_db", "SNR (vs pre-trigger)", lambda v: f"{v:.1f} dB"),
    ("delay_thr_us", "Delay (threshold)", lambda v: f"{v:.1f} µs"),
    ("delay_thr_samples", "  = samples", lambda v: f"{v:d}"),
    ("delay_xc_us", "Delay (x-corr)", lambda v: f"{v:.1f} µs"),
    ("delay_xc_samples", "  = samples", lambda v: f"{v:d}"),
    ("distance_m", "Distance", lambda v: f"{v:.3f} m"),
]
