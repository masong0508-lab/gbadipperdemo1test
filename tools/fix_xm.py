#!/usr/bin/env python3
"""Make an .xm safe for Maxmod's GBA software mixer (16.384 kHz, 8-bit output).

What it fixes (found by analysing the original file):
  1. Samples were 16-bit, full scale, and several carried a DC offset (+0.11..+0.16).
     Ten channels summed to ~5x full scale -> the 8-bit mixer hard-clipped ~10% of all
     output samples (that is the crackle) with a large DC bias on top.
  2. Six instruments were played at 1.4x-3.9x the mixer rate with content up to 22 kHz.
     Maxmod has no anti-alias filter -> harsh aliasing noise. Samples are now low-pass
     filtered + resampled so the highest note steps <= STEP_MAX source samples/output sample.
  3. One-shot samples started/ended off-zero -> clicks. Fades are applied.
  4. Loudness of the 16 instruments differed wildly (RMS 0.09 .. 0.64), so some channels
     drowned others. They are now levelled to a common perceived loudness.
Pitch is preserved exactly: resampling is compensated with relative-note + finetune.

Usage: python3 fix_xm.py in.xm out.xm
"""
import sys, struct, math
from fractions import Fraction
import numpy as np
from scipy.signal import resample_poly, firwin, fftconvolve

sys.path.insert(0, __file__.rsplit('/', 1)[0] if '/' in __file__ else '.')
from xmparse import load

MIX_RATE = 16384.0          # Maxmod MM_MIX_16KHZ (mmInitDefault)
STEP_MAX = 1.5              # max source samples consumed per output sample
LEVEL_MS = 120              # loudness is measured over the first 120 ms of playback

TARGET_RMS = 0.20           # loudness every instrument is levelled to (first 120 ms)
NOISY_TRIM = 0.70           # dense noise-like hits (hats/snare) read louder -> trim
MASTER_HEADROOM = None      # printed by render_check; applied at runtime in main.c

# The 120 ms loudness match above only looks at each note's *attack*. It says
# nothing about how much of the song's total playing time an instrument
# actually occupies. A bass instrument that is played on almost every row ends
# up contributing far more total energy to the mix than a hit that is levelled
# to the same attack loudness but plays rarely -- on real hardware that shows
# up as a low-frequency "rumble" that buries the melody/percussion, even
# though no single note is clipping or too loud on its own. We measure each
# instrument's share of total sounding time and rein in outliers so the mix
# reflects perceived balance, not just per-note attack loudness.
BUSY_TRIM_ABOVE = 2.0        # trim instruments playing >2x the average onset rate
BUSY_TRIM_MIN = 0.5          # never trim a busy instrument by more than -6 dB
BASS_CENTROID_HZ = 1500      # only trim busy instruments whose spectrum sits in
                              # the bass register -- a busy hi-hat/perc instrument
                              # should stay loud, only a busy bassline should duck


def note_rate(note, rel, ft):
    n = note - 1 + rel
    period = 7680 - n * 64 - ft / 2
    return 8363 * 2 ** ((4608 - period) / 768)


def collect_usage(m):
    """playback rates (Hz) of every note per instrument, using the ORIGINAL tuning"""
    use, last = {}, [0] * m['nch']
    for pat in m['pats']:
        for row in pat:
            for c, (note, ins, vol, eff, par) in enumerate(row):
                if ins: last[c] = ins
                if note and note < 97 and last[c]:
                    h, _ = m['insts'][last[c] - 1][0]
                    use.setdefault(last[c], []).append(note_rate(note, h[7], h[4]))
    return use


def lowpass(y, cutoff):
    """zero-phase FIR low-pass; cutoff is a fraction of Nyquist (0..1)"""
    if cutoff >= 0.999: return y
    taps = firwin(127, cutoff, window=('kaiser', 8.0))
    pad = np.pad(y, (63, 63), mode='edge')
    return fftconvolve(pad, taps, mode='valid')


def prepare(x16, hdr, rates):
    """returns (float sample, up, down, median playback rate of new sample)"""
    x = x16 / 32768.0
    x = x - x.mean()                                   # DC removal
    if rates:
        fmax, fmed = max(rates), float(np.median(rates))
        step = fmax / MIX_RATE
        up, down = 1, 1
        if step > STEP_MAX:
            fr = Fraction(STEP_MAX / step).limit_denominator(256)
            up, down = fr.numerator, fr.denominator
            x = resample_poly(x, up, down)
        r = up / down
        step_new = fmax * r / MIX_RATE
        x = lowpass(x, min(1.0, 0.94 / step_new))       # nothing above mixer Nyquist
        fmed_new = fmed * r
    else:                                               # unused instrument: just clean it
        up = down = 1; fmed_new = 8363.0
    n = len(x)
    fi = min(8, max(1, n // 16)); x[:fi] *= np.linspace(0, 1, fi, endpoint=False)
    fo = min(n // 4, max(24, int(0.004 * fmed_new)))
    x[-fo:] *= np.linspace(1, 0, fo)                    # end on zero: no click
    return x, up, down, fmed_new


def build(src, dst):
    raw = open(src, 'rb').read()
    m = load(src)
    use = collect_usage(m)
    hs, songlen, restart, nch, npat, ninst = struct.unpack('<IHHHHH', raw[60:74])

    # pass 1: filter / resample / measure loudness
    prep = []
    for i, sm in enumerate(m['insts'], 1):
        h, x16 = sm[0]
        x, up, down, fmed = prepare(x16, h, use.get(i))
        n120 = max(8, int(fmed * LEVEL_MS / 1000))
        rms = math.sqrt(float((x[:n120] ** 2).mean())) or 1e-6
        tgt = TARGET_RMS * (NOISY_TRIM if rms > 0.5 else 1.0)
        centroid = float((np.abs(np.fft.rfft(x)) * np.fft.rfftfreq(len(x), 1 / MIX_RATE)).sum()
                          / (np.abs(np.fft.rfft(x)).sum() + 1e-9))
        prep.append((x, h, up, down, tgt / rms, centroid))

    # rein in bass-register instruments that are played far more often than
    # everyone else -- their attack-loudness is already matched, but at 3-4x
    # the onset rate they end up dominating the mix's total low-end energy
    # (heard as rumble that buries the melody/percussion). Busy instruments
    # up in the melody/percussion register are left alone -- turning those
    # down would work against the goal, not for it.
    onset_counts = {i: len(use.get(i, [])) for i in range(1, len(m['insts']) + 1)}
    busy = [c for c in onset_counts.values() if c > 0]
    avg_onsets = sum(busy) / len(busy) if busy else 0
    for idx in range(len(prep)):
        i = idx + 1
        n = onset_counts.get(i, 0)
        x, h, up, down, g, centroid = prep[idx]
        if avg_onsets and n > BUSY_TRIM_ABOVE * avg_onsets and centroid < BASS_CENTROID_HZ:
            trim = max(BUSY_TRIM_MIN, avg_onsets / n)
            prep[idx] = (x, h, up, down, g * trim, centroid)

    # pass 2: scale so the loudest instrument peaks at 0.98 of 8-bit full scale,
    # every other instrument keeps its levelled relationship to it
    kmax = max(np.abs(x).max() * g for x, h, up, down, g, c in prep)
    scale = 0.98 / kmax

    # rebuild file
    out = bytearray(raw[:60 + hs])
    pos = 60 + hs
    for p in range(npat):                                # patterns copied verbatim
        hl, pt, rows, ps = struct.unpack('<IBHH', raw[pos:pos + 9]); pos += hl + ps
    out += raw[60 + hs:pos]
    report = []
    for i in range(ninst):
        isz, = struct.unpack('<I', raw[pos:pos + 4]); ns, = struct.unpack('<H', raw[pos + 27:pos + 29])
        ihdr = bytearray(raw[pos:pos + isz]); p2 = pos + isz
        if ns == 0:
            out += ihdr; pos = p2; continue
        shs, = struct.unpack('<I', raw[pos + 29:pos + 33])
        shdr = bytearray(raw[p2:p2 + shs]); old_len = struct.unpack('<I', shdr[:4])[0]
        x, h, up, down, g, centroid = prep[i]
        y = np.clip(np.round(x * g * scale * 127.0), -128, 127).astype(np.int64)
        delta = np.diff(np.r_[0, y]).astype(np.int64)
        data = ((delta + 128) % 256 - 128).astype('<i1').tobytes()
        # keep pitch: samples were resampled by up/down -> shift tuning by 12*log2(up/down)
        total = h[7] + h[4] / 128.0 + 12 * math.log2(up / down)
        rel = int(round(total)); ft = int(round((total - rel) * 128))
        if ft > 127: rel += 1; ft -= 128
        if ft < -128: rel -= 1; ft += 128
        struct.pack_into('<I', shdr, 0, len(y))
        struct.pack_into('<I', shdr, 4, 0); struct.pack_into('<I', shdr, 8, 0)   # no loop
        shdr[12] = 64                                                             # sample volume
        struct.pack_into('<b', shdr, 13, ft)
        shdr[14] = 0                                                              # 8-bit, no loop
        struct.pack_into('<b', shdr, 16, rel)
        out += ihdr + shdr + data
        pos = p2 + shs + old_len   # XM stores length in bytes
        report.append((i + 1, old_len // (2 if h[5] & 16 else 1), len(y), rel, ft, g * scale))
    open(dst, 'wb').write(out)
    print('inst  old_len -> new_len  relnote finetune  bake-gain')
    for r in report: print('%3d  %7d -> %7d  %5d %7d   %.3f' % r)
    print('total sample points: %d -> %d' % (sum(r[1] for r in report), sum(r[2] for r in report)))


if __name__ == '__main__':
    build(sys.argv[1], sys.argv[2])
