"""Save / load RX captures (spec §6.7). No Qt.

.npy + .json : <base>.npy = uint16 ADC codes (12 bits), <base>.json = metadata + sync_indices
.csv         : '# key: <json value>' header lines with the same metadata, then the columns
               sample,time_s,code,v_adc,v_input,sync
               sample = index in the capture, time_s = (sample - pre) / fs (t = 0 at the trigger),
               v_adc = (code - mid_code) * lsb_v (AC volts at the ADC input),
               v_input = v_adc / |G_total(analysis_freq_hz)| (volts at the hydrophone)
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import numpy as np

from . import rx_protocol as P
from .rx_model import FS_HZ, LSB_V, VA

APP_VERSION = "1.0"
CSV_COLUMNS = "sample,time_s,code,v_adc,v_input,sync"


def default_name(freq_hz: float | None = None, now: datetime | None = None) -> str:
    now = now or datetime.now()
    f = f"_{freq_hz / 1e3:.0f}kHz" if freq_hz else ""
    return f"rx_{now:%Y%m%d_%H%M%S}{f}"


def base_metadata() -> dict:
    now = datetime.now(timezone.utc)
    return {"timestamp_utc": now.isoformat(timespec="seconds"),
            "timestamp_local": now.astimezone().isoformat(timespec="seconds"),
            "app_version": APP_VERSION, "fs_hz": FS_HZ, "adc_va_v": VA, "lsb_v": LSB_V}


def _clean(o):
    """Make metadata JSON-safe (NumPy scalars, NaN)."""
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, np.generic):
        o = o.item()
    if isinstance(o, float) and not np.isfinite(o):
        return None
    return o


def split_words(words) -> tuple[np.ndarray, list[int]]:
    """Sample words -> (12-bit codes, indices where SYNC is high)."""
    w = np.asarray(words, dtype=np.uint16)
    return (w & P.CODE_MASK).astype(np.uint16), np.flatnonzero(w & P.SYNC_BIT).tolist()


def save_npy(base: str, words, meta: dict) -> tuple[str, str]:
    base = os.path.splitext(base)[0]
    codes, sync = split_words(words)
    np.save(base + ".npy", codes)
    m = _clean(dict(meta))
    m["n"] = int(codes.size)
    m["sync_indices"] = sync
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(m, f, indent=2)
    return base + ".npy", base + ".json"


def load_npy(base: str) -> tuple[np.ndarray, dict]:
    base = os.path.splitext(base)[0]
    codes = np.load(base + ".npy")
    with open(base + ".json", encoding="utf-8") as f:
        meta = json.load(f)
    return codes, meta


def save_csv(path: str, words, meta: dict, input_gain: float) -> str:
    codes, sync = split_words(words)
    m = _clean(dict(meta))
    m["n"] = int(codes.size)
    m["sync_indices"] = sync
    fs = float(m.get("fs_hz", FS_HZ))
    pre = int(m.get("pre", 0) or 0)
    mid = float(m.get("mid_code", 2048) or 2048)
    lsb = float(m.get("lsb_v", LSB_V))
    s = np.arange(codes.size)
    v_adc = (codes.astype(float) - mid) * lsb
    v_in = v_adc / input_gain if input_gain > 0 else np.full(codes.size, np.nan)
    sync_col = np.zeros(codes.size, dtype=int)
    sync_col[sync] = 1
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for k, v in m.items():
            f.write(f"# {k}: {json.dumps(v)}\n")
        f.write(CSV_COLUMNS + "\n")
        for i in range(codes.size):
            f.write(f"{s[i]},{(s[i] - pre) / fs:.9f},{codes[i]},{v_adc[i]:.7g},{v_in[i]:.7g},{sync_col[i]}\n")
    return path


def load_csv(path: str) -> tuple[np.ndarray, dict]:
    meta: dict = {}
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("# "):
                k, _, v = line[2:].rstrip("\n").partition(": ")
                meta[k] = json.loads(v)
            elif line.startswith("sample,"):
                continue
            elif line.strip():
                rows.append(int(line.split(",", 3)[2]))
    return np.asarray(rows, dtype=np.uint16), meta
