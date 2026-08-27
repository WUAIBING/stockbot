# -*- coding: utf-8 -*-
"""TDX block_gn.dat -> concept name + member codes.

384: uint16 block count; records from 386, 2813 bytes each:
  9 bytes name (GBK), uint16 member count, uint16 level, 400 x 7-byte codes.

The file is a SNAPSHOT of membership as it stands today. A stock joins the
robotics concept once it is visibly a robotics stock, so backtesting a 2019
event against today membership quietly selects the names that went on to
qualify. Every number derived from this is biased upward by that.
"""
import struct, sys, io

def parse(path):
    raw = open(path, "rb").read()
    n = struct.unpack("<H", raw[384:386])[0]
    out = []
    for i in range(n):
        off = 386 + i * 2813
        rec = raw[off:off + 2813]
        if len(rec) < 2813:
            break
        name = rec[0:9].split(b"\x00")[0].decode("gbk", "ignore").strip()
        cnt, _lvl = struct.unpack("<HH", rec[9:13])
        codes = []
        for j in range(min(cnt, 400)):
            c = rec[13 + j * 7: 20 + j * 7].split(b"\x00")[0].decode("ascii", "ignore").strip()
            if c.isdigit() and len(c) == 6:
                codes.append(c)
        if name and codes:
            out.append((name, codes))
    return out

if __name__ == "__main__":
    blocks = parse(sys.argv[1])
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    print("blocks: %d" % len(blocks))
    kw = sys.argv[2] if len(sys.argv) > 2 else ""
    if kw:
        for name, codes in blocks:
            if kw in name:
                print("  %-14s %4d  %s" % (name, len(codes), " ".join(codes[:12])))
    else:
        for name, codes in sorted(blocks, key=lambda b: -len(b[1]))[:40]:
            print("  %-14s %4d" % (name, len(codes)))
