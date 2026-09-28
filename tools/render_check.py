"""Offline approximation of the Maxmod GBA mixer (16.384 kHz, nearest-neighbour,
no interpolation, 8-bit-range hard clip). Used to compare the original song against
the GBA-optimised one: clipping %, per-channel loudness, aliasing risk."""
import sys, numpy as np
sys.path.insert(0, __file__.rsplit('/',1)[0])
from xmparse import load

MIX = 16384.0

def render(path, master=1.0, max_rows=None):
    m = load(path); nch = m['nch']
    rowsec = m['speed'] * 2.5 / m['bpm']
    spr = rowsec * MIX                       # samples per row
    rows = [(o, r) for o in m['order'] for r in range(len(m['pats'][o]))]
    if max_rows: rows = rows[:max_rows]
    total = int(len(rows) * spr) + int(MIX * 3)
    stems = np.zeros((nch, total))
    lastins = [0]*nch
    for ri, (o, r) in enumerate(rows):
        start = int(round(ri * spr))
        for c, (note, ins, vol, eff, par) in enumerate(m['pats'][o][r]):
            if ins: lastins[c] = ins
            if not note or note >= 97 or not lastins[c]: continue
            h, x = m['insts'][lastins[c]-1][0]
            n = note - 1 + h[7]
            period = 7680 - n*64 - h[4]/2
            fp = 8363 * 2**((4608 - period)/768)
            v = (vol - 0x10)/64 if 0x10 <= vol <= 0x50 else h[3]/64
            step = fp / MIX
            cnt = int(len(x) / step)
            idx = (np.arange(cnt) * step).astype(int)
            seg = x[idx] / 32768.0 * v
            # a new note on a channel cuts the previous one (XM behaviour)
            stems[c, start:] *= 0  if False else 1
            end = min(total, start + cnt)
            # cut previous voice at this point
            stems[c, start:] = 0
            stems[c, start:end] = seg[:end-start]
    return stems

def report(stems, master, label):
    mix = stems.sum(axis=0) * master
    active = mix[np.abs(stems).sum(axis=0) > 1e-4]
    clip = (np.abs(active) > 1.0).mean() * 100
    print(f'[{label}] master={master:.2f}  peak={np.abs(mix).max():.2f}  RMS={np.sqrt((active**2).mean()):.3f}  clipped samples={clip:.2f}%  DC={active.mean():+.3f}')
    per = np.array([np.sqrt((s[np.abs(s) > 1e-4]**2).mean()) if (np.abs(s) > 1e-4).any() else 0 for s in stems])
    print('   per-channel RMS while sounding:', np.round(per*master, 3).tolist())
    share = np.array([(s**2).sum() for s in stems]); share = share/share.sum()*100
    print('   per-channel share of total energy %:', np.round(share, 1).tolist())
    return mix

if __name__ == '__main__':
    path = sys.argv[1]; master = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0
    s = render(path, 1.0)
    mix = report(s, master, path.split('/')[-1])
