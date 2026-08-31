#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Which sector a stock is in, and where it ranks inside that sector.

The decision path has never known this. `industry` reads "unknown" on all 2,507
records in v10_model_decisions.jsonl, and the scan CSV has no industry column at
all. The `sector` score component is therefore not a sector measure: across
those 2,507 decisions it takes 20 distinct values against 1,067 for `stock` and
918 for `flow`, and on 2026-08-31 EVERY candidate scored exactly 60.41. It is
one number per day. It shifts the whole day up or down and cannot rank one
stock against another.

That left the book unable to see its own concentration. On 2026-08-31 three of
five buys - 300747 锐科激光, 688700 东威科技, 688596 正帆科技 - were all
T070506 专用机械, and nothing in the system could say so.

WHY RANK, NOT JUST NAME

Measured over 24,773 disclosure events, ranking each name by turnover inside its
own sector separated the outcome, but only once each band was compared against
its own placebo - raw excess against an equal-weighted universe carries a
structural drag for large names that reverses the ordering:

    band        K=3            K=5            K=10          stocks
    top 1-3   +0.91 t=3.03   +0.49 t=1.30   -0.21 t=-0.47    1,084
    4-10      +0.53 t=2.73   +0.71 t=2.64   +0.94 t=2.45     2,342
    11-30     +0.49 t=3.42   +0.69 t=3.79   +0.74 t=3.00     5,082
    31+       +0.37 t=2.18   +0.54 t=2.33   +0.50 t=1.70    11,679

The leaders carry about 2.5x the tail at three sessions. None of that was
applicable while every stock's sector was "unknown".

DATA, AND ITS LIMITS

tdxhy.cfg maps code to a TDX industry code (market|code|tdx_hy|||csrc_hy) and
incon.dat's TDXNHY section names them. Both already ship on the box under
csi1000-skills. The mapping is a SNAPSHOT: a newly listed code is absent until
the file is refreshed, so sector_of returns None rather than guessing, and every
caller must treat unknown as unknown rather than as a bucket.

The rank uses whatever turnover the caller supplies. The measurement that
validated ranking used a TRAILING 20-SESSION MEAN; a single session's turnover
is a noisier proxy for the same thing, so a rank built from one day's figure is
weaker evidence than the table above. It is quoted as a proxy, not as the
measured construct.
"""

from __future__ import annotations

import os
from typing import Iterable, Mapping

# Second tier of the TDX code: T1102 out of T110201. Tight enough to have a
# recognisable leader, broad enough not to fragment into groups of three.
DEFAULT_LEVEL = 5

_TDXHY_CANDIDATES = (
    "/opt/stockbot/workbuddy/skills/csi1000-skills/tdxhy.cfg",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "skills", "csi1000-skills", "tdxhy.cfg"),
)
_INCON_CANDIDATES = (
    "/opt/stockbot/workbuddy/skills/csi1000-skills/incon.dat",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "skills", "csi1000-skills", "incon.dat"),
)


def _first_existing(paths):
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None


def load_code_sectors(path=None) -> dict:
    """code -> full TDX industry code. Line is market|code|tdx_hy|||csrc_hy."""
    path = path or _first_existing(_TDXHY_CANDIDATES)
    if not path:
        return {}
    out = {}
    try:
        with open(path, encoding="gbk", errors="ignore") as fh:
            for line in fh:
                parts = line.strip().split("|")
                if len(parts) < 3:
                    continue
                code, hy = parts[1].strip(), parts[2].strip()
                if len(code) == 6 and code.isdigit() and hy.startswith("T"):
                    out[code] = hy
    except OSError:
        return {}
    return out


def load_sector_names(path=None) -> dict:
    """TDX industry code -> name, from incon.dat's TDXNHY section.

    incon.dat holds several classification systems (ZJHHY, TDXNHY, SWHY ...).
    Only TDXNHY matches tdxhy.cfg, so reading the wrong section would name every
    sector plausibly and wrongly.
    """
    path = path or _first_existing(_INCON_CANDIDATES)
    if not path:
        return {}
    out = {}
    try:
        with open(path, encoding="gbk", errors="ignore") as fh:
            raw = fh.read()
    except OSError:
        return {}
    section = None
    for line in raw.splitlines():
        s = line.strip()
        if s.startswith("#"):
            section = s[1:].strip()
            continue
        if section != "TDXNHY" or "|" not in s:
            continue
        code, _, name = s.partition("|")
        code, name = code.strip(), name.strip()
        if code.startswith("T") and name:
            out[code] = name
    return out


class SectorMap:
    """code -> sector, with names, loaded once."""

    def __init__(self, code_sectors=None, sector_names=None, level=DEFAULT_LEVEL):
        self.level = int(level)
        self._codes = dict(code_sectors if code_sectors is not None
                           else load_code_sectors())
        self._names = dict(sector_names if sector_names is not None
                           else load_sector_names())

    def __len__(self):
        return len(self._codes)

    def sector_of(self, code):
        """(sector_code, sector_name) or None.

        None means the file has not seen this listing - newly listed codes are
        absent until it is refreshed. Callers must keep that as unknown rather
        than folding it into a bucket, or every new listing silently becomes one
        large fake sector.
        """
        full = self._codes.get(str(code or "").strip().zfill(6))
        if not full:
            return None
        sec = full[: self.level]
        return sec, self._names.get(sec) or self._names.get(full) or sec

    def sector_code(self, code):
        hit = self.sector_of(code)
        return hit[0] if hit else None

    def sector_name(self, code):
        hit = self.sector_of(code)
        return hit[1] if hit else None

    def members(self, sector_code) -> list:
        sec = str(sector_code or "")
        return sorted(c for c, full in self._codes.items()
                      if full[: self.level] == sec)


def rank_within_sector(code, turnover_by_code: Mapping, smap: SectorMap,
                       *, min_members: int = 5) -> dict:
    """1-based turnover rank of a stock among its sector peers.

    turnover_by_code supplies the figure - the 09:31 tradability file carries
    amount for the whole tradable universe, which is the broadest source on the
    box. That is ONE session, while the measurement behind the bands used a
    trailing 20-session mean, so this is a proxy and the result says so.

    Declines rather than guessing when the sector is unknown or too thin to
    rank: being 1 of 3 is not leadership.
    """
    code = str(code or "").strip().zfill(6)
    hit = smap.sector_of(code)
    if not hit:
        return {"code": code, "rank": None, "reason": "sector unknown for this code"}
    sec, name = hit
    peers = []
    for peer in smap.members(sec):
        try:
            amt = float(turnover_by_code.get(peer))
        except (TypeError, ValueError):
            continue
        if amt > 0:
            peers.append((peer, amt))
    if len(peers) < min_members:
        return {"code": code, "sector": sec, "sector_name": name, "rank": None,
                "peers": len(peers),
                "reason": "only %d peers with turnover, need %d"
                          % (len(peers), min_members)}
    mine = dict(peers).get(code)
    if mine is None:
        return {"code": code, "sector": sec, "sector_name": name, "rank": None,
                "peers": len(peers), "reason": "no turnover for this code today"}
    rank = 1 + sum(1 for _, amt in peers if amt > mine)
    return {
        "code": code, "sector": sec, "sector_name": name,
        "rank": rank, "peers": len(peers),
        "turnover": mine,
        "basis": "single_session_turnover_proxy",
        "reason": "rank %d of %d by session turnover" % (rank, len(peers)),
    }


def concentration(codes: Iterable, smap: SectorMap) -> list:
    """How a basket splits across sectors, commonest first.

    Three of five buys on 2026-08-31 were T070506 专用机械 and nothing could
    report it.
    """
    tally = {}
    for c in codes or []:
        hit = smap.sector_of(c)
        key = hit[0] if hit else "unknown"
        name = hit[1] if hit else "unknown"
        entry = tally.setdefault(key, {"sector": key, "sector_name": name,
                                       "codes": []})
        entry["codes"].append(str(c).zfill(6))
    out = sorted(tally.values(), key=lambda e: (-len(e["codes"]), e["sector"]))
    for e in out:
        e["count"] = len(e["codes"])
    return out
