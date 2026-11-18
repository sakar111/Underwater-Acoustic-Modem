"""SNR / SINAD of an RX capture saved with "Save capture .npy" (RX spec §9.2 step 8).

    python tools/rx_snr.py rx_20261118_101500_40kHz.npy

SNR excludes DC and harmonics 2..6 (aliased into 0..200 kHz); SINAD includes them.
Pass: SNR >= 55 dB for a clean ~4 Vpp 40 kHz tone, N = 65 536.
"""
import sys

import numpy as np

FS = 400_000.0
LSB = 5.0 / 4096


def analyse(codes: np.ndarray, fs: float = FS, n_harm: int = 6, half: int = 8):
    x = codes.astype(float)
    x -= x.mean()
    n = x.size
    k = np.arange(n) * 2 * np.pi / (n - 1)    # 4-term Blackman-Harris, sidelobes < -92 dB
    w = 0.35875 - 0.48829 * np.cos(k) + 0.14128 * np.cos(2 * k) - 0.01168 * np.cos(3 * k)
    p = np.abs(np.fft.rfft(x * w)) ** 2
    f = np.fft.rfftfreq(n, 1 / fs)
    p[:half + 1] = 0                        # DC
    k0 = int(np.argmax(p))
    f0 = f[k0]

    def bins_at(freq):
        freq = freq % fs
        freq = min(freq, fs - freq)         # alias into 0..fs/2
        k = int(round(freq / (fs / n)))
        return range(max(k - half, 0), min(k + half + 1, p.size))

    sig = list(bins_at(f0))
    harm = sorted({b for h in range(2, n_harm + 1) for b in bins_at(h * f0)} - set(sig))
    mask = np.ones(p.size, bool)
    mask[:half + 1] = False
    mask[sig] = False
    p_sig = p[sig].sum()
    p_harm = p[harm].sum()
    mask_noise = mask.copy()
    mask_noise[harm] = False
    p_noise = p[mask_noise].sum() * mask.sum() / max(mask_noise.sum(), 1)   # fill the harmonic bins
    snr = 10 * np.log10(p_sig / p_noise)
    sinad = 10 * np.log10(p_sig / (p_noise + p_harm))
    enob = (sinad - 1.76) / 6.02
    vpp = (codes.max() - codes.min()) * LSB
    return f0, vpp, snr, sinad, enob


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    codes = np.load(sys.argv[1])
    f0, vpp, snr, sinad, enob = analyse(codes)
    print(f"N = {codes.size}   tone = {f0 / 1e3:.2f} kHz   ADC = {vpp:.2f} Vpp")
    print(f"SNR   = {snr:.1f} dB   {'PASS' if snr >= 55 else 'FAIL'} (>= 55 dB)")
    print(f"SINAD = {sinad:.1f} dB   ENOB = {enob:.2f} bits")
    if codes.size < 65536:
        print("note: capture shorter than 65 536 samples")
    if vpp < 3.0:
        print("note: tone below ~3 Vpp, SNR reads low; raise the level toward 4 Vpp")


if __name__ == "__main__":
    main()
