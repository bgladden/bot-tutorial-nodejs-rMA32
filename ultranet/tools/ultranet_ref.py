#!/usr/bin/env python3
"""
Behringer Ultranet reference model: encoder, line-level decoder, demultiplexer,
and logic-analyzer capture analyser.

This file is the "executable spec" for the protocol as documented in
../docs/PROTOCOL.md.  Everything else in this project (the PIO decoder, the
firmware core) is tested against it.

Protocol summary (one twisted pair = one "stream" = 8 channels):

  * Line code: AES3 / S/PDIF biphase-mark, 64 unit intervals (UI) per subframe.
  * Rate: AES3 "192 kHz stereo" timing -> 384 000 subframes/s,
          12.288 Mbit/s, 24.576 MBd (UI = 40.69 ns).
  * Each 48 kHz sample period carries 8 subframes = 8 channels.
  * Preambles: X (channel A) / Y (channel B) alternate; Z replaces X every
    192 AES3 frames (block start), exactly like normal AES3.
  * 24-bit sample field (AES3 time slots 4..27, LSB first):
        bits[23:2] = 22-bit two's-complement audio
        bits[1:0]  = channel-PAIR tag: 0 -> ch1/2, 1 -> ch3/4, 2 -> ch5/6, 3 -> ch7/8
    The odd channel of each pair rides on the X/Z subframe, the even one on Y.
    => channel index (0..7) = 2*tag + (preamble == Y)
  * V, U, C, P in slots 28..31; P is even parity over slots 4..31.

Run `python3 ultranet_ref.py --help` for the command line tools.
"""

from __future__ import annotations

import argparse
import bisect
import math
import random
import struct
import sys
import wave
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

FS = 48_000                     # audio sample rate per channel
CHANNELS_PER_STREAM = 8
SUBFRAMES_PER_SEC = FS * CHANNELS_PER_STREAM       # 384 000
BIT_RATE = SUBFRAMES_PER_SEC * 32                  # 12.288 Mbit/s
UI_RATE = BIT_RATE * 2                             # 24.576 MBd
UI_SECONDS = 1.0 / UI_RATE                         # ~40.69 ns
AES3_FRAMES_PER_BLOCK = 192

# Preambles as 8 UI levels, for a line that was LOW before the preamble.
# (If the line was high, every level is inverted - biphase is polarity-free.)
PREAMBLE_UI = {
    "X": (1, 1, 1, 0, 0, 0, 1, 0),   # a.k.a. "M"  - channel A
    "Y": (1, 1, 1, 0, 0, 1, 0, 0),   # a.k.a. "W"  - channel B
    "Z": (1, 1, 1, 0, 1, 0, 0, 0),   # a.k.a. "B"  - channel A + block start
}
# Same preambles expressed as run lengths in UI (polarity independent)
PREAMBLE_RUNS = {"X": (3, 3, 1, 1), "Y": (3, 2, 1, 2), "Z": (3, 1, 1, 3)}

# 4-bit codes the RP2350 PIO decoder puts in bits[3:0] of every word.
PIO_CODE = {"X": 0x9, "Y": 0x6, "Z": 0xF}
PIO_CODE_TO_PRE = {v: k for k, v in PIO_CODE.items()}

AUDIO_BITS = 22
AUDIO_MIN = -(1 << (AUDIO_BITS - 1))
AUDIO_MAX = (1 << (AUDIO_BITS - 1)) - 1


def parity32(x: int) -> int:
    return bin(x & 0xFFFFFFFF).count("1") & 1


# --------------------------------------------------------------------------
# Subframe model
# --------------------------------------------------------------------------

@dataclass
class Subframe:
    pre: str                 # "X", "Y" or "Z"
    data28: int              # AES3 time slots 4..31, bit0 = slot 4

    # ---- derived fields ----
    @property
    def field24(self) -> int:
        return self.data28 & 0xFFFFFF

    @property
    def tag(self) -> int:
        return self.data28 & 0x3

    @property
    def audio22(self) -> int:
        v = (self.data28 >> 2) & 0x3FFFFF
        return v - (1 << 22) if v & (1 << 21) else v

    @property
    def v(self) -> int:
        return (self.data28 >> 24) & 1

    @property
    def u(self) -> int:
        return (self.data28 >> 25) & 1

    @property
    def c(self) -> int:
        return (self.data28 >> 26) & 1

    @property
    def p(self) -> int:
        return (self.data28 >> 27) & 1

    @property
    def parity_ok(self) -> bool:
        return parity32(self.data28) == 0

    @property
    def channel(self) -> int:
        """0-based channel within the stream."""
        return self.tag * 2 + (1 if self.pre == "Y" else 0)

    def to_pio_word(self) -> int:
        """The 32-bit word the PIO decoder pushes: code in [3:0], slots 4..31 in [31:4]."""
        return (PIO_CODE[self.pre] | (self.data28 << 4)) & 0xFFFFFFFF

    @staticmethod
    def from_pio_word(w: int) -> "Subframe":
        pre = PIO_CODE_TO_PRE.get(w & 0xF)
        if pre is None:
            raise ValueError(f"bad preamble code 0x{w & 0xF:x}")
        return Subframe(pre, (w >> 4) & 0xFFFFFFF)


def make_subframe(pre: str, channel: int, audio22: int, v=0, u=0, c=0) -> Subframe:
    if not AUDIO_MIN <= audio22 <= AUDIO_MAX:
        raise ValueError("audio22 out of range")
    tag = channel // 2
    field24 = ((audio22 & 0x3FFFFF) << 2) | tag
    d = field24 | (v << 24) | (u << 25) | (c << 26)
    d |= parity32(d) << 27                       # even parity over slots 4..31
    return Subframe(pre, d)


# --------------------------------------------------------------------------
# Encoder (what a Behringer transmitter does, per our understanding)
# --------------------------------------------------------------------------

def encode_subframes(frames: Sequence[Sequence[int]],
                     channel_status: Optional[Sequence[int]] = None,
                     start_aes_frame: int = 0) -> List[Subframe]:
    """frames: list of 48 kHz sample periods, each 8 x audio22 ints.

    channel_status: optional 192-entry list of C bits (per AES3 frame); the
    same C bit is sent on both subframes of an AES3 frame.
    """
    out: List[Subframe] = []
    sf_index = start_aes_frame * 2
    for fr in frames:
        if len(fr) != 8:
            raise ValueError("each frame needs 8 samples")
        for ch in range(8):
            aes_frame = sf_index // 2
            if sf_index % 2 == 0:
                pre = "Z" if aes_frame % AES3_FRAMES_PER_BLOCK == 0 else "X"
            else:
                pre = "Y"
            cbit = channel_status[aes_frame % 192] if channel_status else 0
            out.append(make_subframe(pre, ch, fr[ch], c=cbit))
            sf_index += 1
    return out


def subframes_to_ui(subframes: Iterable[Subframe], start_level: int = 0) -> List[int]:
    """Biphase-mark encode to a list of line levels, one per UI."""
    ui: List[int] = []
    level = start_level
    for sf in subframes:
        pat = PREAMBLE_UI[sf.pre]
        if level == 1:
            pat = tuple(1 - x for x in pat)
        ui.extend(pat)
        level = pat[-1]
        d = sf.data28
        for i in range(28):
            level ^= 1                  # transition at every cell start
            ui.append(level)
            if (d >> i) & 1:
                level ^= 1              # extra mid-cell transition for '1'
            ui.append(level)
    return ui


def ui_to_edges(ui: Sequence[int], ui_seconds: float = UI_SECONDS,
                jitter_rms: float = 0.0, rng: Optional[random.Random] = None,
                t0: float = 0.0) -> Tuple[int, List[float]]:
    """Return (initial_level, edge_times) for a UI level list."""
    rng = rng or random.Random(1)
    edges = []
    for i in range(1, len(ui)):
        if ui[i] != ui[i - 1]:
            t = t0 + i * ui_seconds
            if jitter_rms:
                t += rng.gauss(0.0, jitter_rms)
            edges.append(t)
    return ui[0], edges


class EdgeSignal:
    """A digital signal described by its initial level and edge times."""

    def __init__(self, initial: int, edges: Sequence[float]):
        self.initial = initial
        self.edges = list(edges)

    def level_at(self, t: float) -> int:
        n = bisect.bisect_right(self.edges, t)
        return self.initial ^ (n & 1)


# --------------------------------------------------------------------------
# Line decoder (software, works on edge timestamps - used for real captures)
# --------------------------------------------------------------------------

@dataclass
class LineDecodeStats:
    runs: int = 0
    ui_seconds: float = 0.0
    bad_runs: int = 0
    resyncs: int = 0
    subframes: int = 0


def estimate_ui(run_lengths: Sequence[float]) -> float:
    """Estimate the UI from a list of run durations (1, 2 and 3 UI runs)."""
    if not run_lengths:
        raise ValueError("no edges")
    srt = sorted(run_lengths)
    # 1-UI runs are the shortest cluster; take the median of the bottom 20%
    lo = srt[: max(1, len(srt) // 5)]
    guess = lo[len(lo) // 2]
    # refine: average of everything classified as n*UI, divided by n
    acc, n = 0.0, 0
    for r in run_lengths:
        k = round(r / guess)
        if 1 <= k <= 3:
            acc += r / k
            n += 1
    return acc / n if n else guess


def decode_edges(edges: Sequence[float], ui_seconds: Optional[float] = None,
                 stats: Optional[LineDecodeStats] = None) -> List[Subframe]:
    """Decode biphase-mark edge timestamps into subframes."""
    stats = stats or LineDecodeStats()
    runs_t = [b - a for a, b in zip(edges, edges[1:])]
    ui = ui_seconds or estimate_ui(runs_t)
    stats.ui_seconds = ui
    runs = []
    for r in runs_t:
        k = int(round(r / ui))
        if k < 1 or k > 3 or abs(r / ui - k) > 0.45:
            k = 0                                   # invalid
            stats.bad_runs += 1
        runs.append(k)
    stats.runs = len(runs)

    out: List[Subframe] = []
    i, n = 0, len(runs)
    while i < n:
        if runs[i] != 3:
            i += 1
            continue
        tail = tuple(runs[i + 1:i + 4])
        pre = next((p for p, r in PREAMBLE_RUNS.items() if r[1:] == tail), None)
        if pre is None:
            stats.resyncs += 1
            i += 1
            continue
        j = i + 4
        data, ok = 0, True
        for bit in range(28):
            if j >= n:
                ok = False
                break
            if runs[j] == 2:
                j += 1
            elif runs[j] == 1 and j + 1 < n and runs[j + 1] == 1:
                data |= 1 << bit
                j += 2
            else:
                ok = False
                break
        if not ok:
            stats.resyncs += 1
            i += 1
            continue
        out.append(Subframe(pre, data))
        i = j
    stats.subframes = len(out)
    return out


# --------------------------------------------------------------------------
# Demultiplexer + protocol conformance checks
# --------------------------------------------------------------------------

@dataclass
class DemuxReport:
    subframes: int = 0
    frames: int = 0
    parity_errors: int = 0
    order_errors: int = 0          # channel index not previous+1
    tag_pre_mismatch: int = 0      # e.g. two X subframes in a row
    z_count: int = 0
    z_on_ch1: int = 0
    z_spacing: List[int] = field(default_factory=list)   # subframes between Zs
    v_set: int = 0
    u_set: int = 0
    cbits: Dict[int, List[int]] = field(default_factory=dict)  # per channel C-bit stream

    def text(self) -> str:
        lines = [
            f"subframes decoded       : {self.subframes}",
            f"complete 8-ch frames    : {self.frames}",
            f"parity errors           : {self.parity_errors}",
            f"channel order errors    : {self.order_errors}",
            f"X/Y alternation errors  : {self.tag_pre_mismatch}",
            f"Z preambles             : {self.z_count} (on channel 1: {self.z_on_ch1})",
        ]
        if self.z_spacing:
            sp = sorted(set(self.z_spacing))
            lines.append(f"Z spacing (subframes)   : {sp[:6]}{' ...' if len(sp) > 6 else ''}"
                         f"  (AES3 expects 384)")
        lines.append(f"subframes with V=1 / U=1 : {self.v_set} / {self.u_set}")
        return "\n".join(lines)


def demux(subframes: Sequence[Subframe]) -> Tuple[List[List[Optional[int]]], DemuxReport]:
    """Return (frames, report).  frames[i][ch] = audio22 or None."""
    rep = DemuxReport(subframes=len(subframes))
    frames: List[List[Optional[int]]] = []
    cur: List[Optional[int]] = [None] * 8
    prev_ch = None
    prev_pre = None
    last_z = None
    for idx, sf in enumerate(subframes):
        if not sf.parity_ok:
            rep.parity_errors += 1
        ch = sf.channel
        if prev_ch is not None and ch != (prev_ch + 1) % 8:
            rep.order_errors += 1
        if prev_pre is not None and (sf.pre == "Y") == (prev_pre == "Y"):
            rep.tag_pre_mismatch += 1
        if sf.pre == "Z":
            rep.z_count += 1
            if ch == 0:
                rep.z_on_ch1 += 1
            if last_z is not None:
                rep.z_spacing.append(idx - last_z)
            last_z = idx
        rep.v_set += sf.v
        rep.u_set += sf.u
        rep.cbits.setdefault(ch, []).append(sf.c)
        cur[ch] = sf.audio22
        if ch == 7:
            if all(x is not None for x in cur):
                frames.append(list(cur))
                rep.frames += 1
            cur = [None] * 8
        prev_ch, prev_pre = ch, sf.pre
    return frames, rep


def channel_status_blocks(subframes: Sequence[Subframe], channel: int = 0) -> List[bytes]:
    """Collect 192-bit AES3 channel-status blocks (24 bytes) for one channel,
    aligned on Z preambles.  Useful to reverse-engineer what Behringer sends."""
    blocks, bits, started = [], [], False
    for sf in subframes:
        if sf.channel != channel and not (sf.pre == "Z"):
            continue
        if sf.pre == "Z":
            if started and len(bits) >= 192:
                blocks.append(_bits_to_bytes(bits[:192]))
            bits, started = [], True
        if started and sf.channel == channel:
            bits.append(sf.c)
    return blocks


def _bits_to_bytes(bits: Sequence[int]) -> bytes:
    out = bytearray()
    for i in range(0, len(bits), 8):
        b = 0
        for k, bit in enumerate(bits[i:i + 8]):
            b |= bit << k                 # AES3 channel status is LSB first
        out.append(b)
    return bytes(out)


# --------------------------------------------------------------------------
# Capture loaders
# --------------------------------------------------------------------------

def load_edges_csv(path: str, column: int = 1) -> List[float]:
    """Saleae Logic 2 / sigrok CSV: first column = time (s), others = 0/1."""
    edges, last = [], None
    with open(path) as f:
        for line in f:
            parts = [p.strip() for p in line.replace(";", ",").split(",")]
            try:
                t = float(parts[0])
                v = int(float(parts[column]))
            except (ValueError, IndexError):
                continue                       # header or junk
            if last is not None and v != last:
                edges.append(t)
            last = v
    return edges


def load_edges_binary(path: str, rate: float, bit: int = 0, bytes_per_sample: int = 1) -> List[float]:
    """sigrok-cli -O binary output: one little-endian word per sample."""
    edges = []
    with open(path, "rb") as f:
        data = f.read()
    last = None
    step = bytes_per_sample
    for i in range(0, len(data) - step + 1, step):
        v = (int.from_bytes(data[i:i + step], "little") >> bit) & 1
        if last is not None and v != last:
            edges.append(i // step / rate)
        last = v
    return edges


def write_wav(path: str, frames: Sequence[Sequence[Optional[int]]]) -> None:
    """8-channel 32-bit WAV, 22-bit audio left-justified."""
    with wave.open(path, "wb") as w:
        w.setnchannels(8)
        w.setsampwidth(4)
        w.setframerate(FS)
        buf = bytearray()
        for fr in frames:
            for s in fr:
                buf += struct.pack("<i", ((s or 0) << 10))
        w.writeframes(bytes(buf))


# --------------------------------------------------------------------------
# Test signal helpers
# --------------------------------------------------------------------------

def test_tone_frames(n: int, seed: int = 0) -> List[List[int]]:
    """n frames; channel k carries a sine of (k+1)*1 kHz plus a little noise,
    so every channel is distinguishable."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        fr = []
        for ch in range(8):
            v = 0.5 * math.sin(2 * math.pi * (ch + 1) * 1000 * i / FS)
            s = int(v * AUDIO_MAX) + rng.randint(-3, 3)
            fr.append(max(AUDIO_MIN, min(AUDIO_MAX, s)))
        out.append(fr)
    return out


def random_frames(n: int, seed: int = 0) -> List[List[int]]:
    rng = random.Random(seed)
    return [[rng.randint(AUDIO_MIN, AUDIO_MAX) for _ in range(8)] for _ in range(n)]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _cmd_selftest(_args) -> int:
    frames = random_frames(400, seed=7) + [[0] * 8] * 20 + [[-1] * 8] * 20
    sfs = encode_subframes(frames)
    for start_level in (0, 1):
        ui = subframes_to_ui(sfs, start_level)
        init, edges = ui_to_edges(ui, jitter_rms=1.5e-9, rng=random.Random(3))
        dec = decode_edges(edges)
        # The first subframe has no edge in front of its preamble and the last
        # one has no closing edge, so a capture always loses one at each end.
        assert len(dec) == len(sfs) - 2, (len(dec), len(sfs))
        assert [s.to_pio_word() for s in dec] == [s.to_pio_word() for s in sfs[1:-1]], "line decode mismatch"
        got, rep = demux(dec)
        assert rep.parity_errors == 0 and rep.order_errors == 0, rep.text()
        assert got == frames[1:-1], "demux mismatch"
    print("selftest OK: encode -> biphase -> jittered edges -> decode -> demux round-trips")
    return 0


def _cmd_analyze(args) -> int:
    if args.format == "csv":
        edges = load_edges_csv(args.capture, args.column)
    else:
        edges = load_edges_binary(args.capture, args.rate, args.bit, args.bytes_per_sample)
    st = LineDecodeStats()
    sfs = decode_edges(edges, stats=st)
    print(f"edges                   : {len(edges)}")
    if st.ui_seconds:
        print(f"estimated UI            : {st.ui_seconds * 1e9:.2f} ns "
              f"(-> {1 / st.ui_seconds / 64 / 8:.1f} Hz sample rate; 48000 expected)")
    print(f"invalid run lengths     : {st.bad_runs}")
    print(f"resyncs                 : {st.resyncs}")
    frames, rep = demux(sfs)
    print(rep.text())
    for ch in range(8):
        vals = [f[ch] for f in frames if f[ch] is not None]
        if vals:
            pk = max(abs(v) for v in vals) or 1
            print(f"  ch{ch + 1}: peak {20 * math.log10(pk / AUDIO_MAX):6.1f} dBFS")
    if args.cs:
        for blk in channel_status_blocks(sfs)[:2]:
            print("  channel status ch1:", blk.hex(" "))
    if args.wav:
        write_wav(args.wav, frames)
        print(f"wrote {len(frames)} frames to {args.wav}")
    return 0


def _cmd_gen(args) -> int:
    """Write a synthetic capture (CSV of edges) for testing the analyser."""
    frames = test_tone_frames(args.frames)
    ui = subframes_to_ui(encode_subframes(frames))
    init, edges = ui_to_edges(ui, jitter_rms=args.jitter_ns * 1e-9)
    with open(args.out, "w") as f:
        f.write("Time [s],Channel 0\n")
        f.write(f"0.0,{init}\n")
        lvl = init
        for t in edges:
            lvl ^= 1
            f.write(f"{t:.12f},{lvl}\n")
    print(f"wrote {len(edges)} edges to {args.out}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest", help="round-trip the model")
    a = sub.add_parser("analyze", help="decode a logic-analyzer capture of ONE Ultranet pair")
    a.add_argument("capture")
    a.add_argument("--format", choices=["csv", "binary"], default="csv")
    a.add_argument("--column", type=int, default=1, help="CSV column holding the signal")
    a.add_argument("--rate", type=float, default=200e6, help="binary: sample rate")
    a.add_argument("--bit", type=int, default=0, help="binary: bit holding the signal")
    a.add_argument("--bytes-per-sample", type=int, default=1)
    a.add_argument("--wav", help="write decoded audio to an 8-channel WAV")
    a.add_argument("--cs", action="store_true", help="dump channel-status blocks")
    g = sub.add_parser("gen", help="generate a synthetic CSV capture")
    g.add_argument("out")
    g.add_argument("--frames", type=int, default=480)
    g.add_argument("--jitter-ns", type=float, default=2.0)
    args = ap.parse_args(argv)
    return {"selftest": _cmd_selftest, "analyze": _cmd_analyze, "gen": _cmd_gen}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
