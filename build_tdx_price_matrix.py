# -*- coding: utf-8 -*-
"""Parse TDX .day files into one aligned price matrix.

Alignment is by DATE, not by row index: a stock that was halted has fewer rows
than the market, and index arithmetic on that silently compares two different
calendar windows.
"""
import glob, os, sys
import numpy as np

VIPDOC = r"D:\new_tdx\vipdoc"
START = 20150101
REC = np.dtype([('date','<u4'),('open','<u4'),('high','<u4'),('low','<u4'),
                ('close','<u4'),('amount','<f4'),('vol','<u4'),('res','<u4')])

files = []
for mk in ("sh","sz","bj"):
    files += sorted(glob.glob(os.path.join(VIPDOC, mk, "lday", "*.day")))
print("files:", len(files), flush=True)

per = {}
alldates = set()
for n, path in enumerate(files):
    code = os.path.basename(path)[2:8]
    if not code.isdigit():
        continue
    try:
        a = np.fromfile(path, dtype=REC)
    except Exception:
        continue
    if a.size == 0:
        continue
    a = a[a['date'] >= START]
    if a.size < 60:
        continue
    d = a['date'].astype(np.int64)
    c = a['close'].astype(np.float64) / 100.0
    amt = a['amount'].astype(np.float64)
    ok = c > 0
    if ok.sum() < 60:
        continue
    per[code] = (d[ok], c[ok], amt[ok])
    alldates.update(d[ok].tolist())
    if n % 1500 == 0:
        print("  parsed", n, flush=True)

dates = np.array(sorted(alldates), dtype=np.int64)
pos = {int(v): i for i, v in enumerate(dates)}
codes = sorted(per)
C = np.full((len(codes), len(dates)), np.nan, dtype=np.float32)
A = np.full((len(codes), len(dates)), np.nan, dtype=np.float32)
for i, code in enumerate(codes):
    d, c, amt = per[code]
    idx = np.array([pos[int(x)] for x in d], dtype=np.int64)
    C[i, idx] = c
    A[i, idx] = amt

out = sys.argv[1]
np.savez_compressed(out, dates=dates, codes=np.array(codes), close=C, amount=A)
print("stocks:", len(codes), "sessions:", len(dates),
      "range:", dates[0], "-", dates[-1], flush=True)
