#!/usr/bin/env python3
"""
Tests for the Ultranet project.

    python3 -m unittest -v test_ultranet          (from ultranet/tools)

1. Reference model round trip (encoder <-> software decoder).
2. Firmware core (C, compiled for the host) against the reference model.
3. End-to-end, cycle-level: synthetic Ultranet pair A + pair B
      -> emulated PIO decoder (the real ultranet.pio)
      -> firmware core (the real ultranet_core.c via ctypes)
      -> emulated 8-line I2S PIO (the real ultranet.pio)
      -> I2S pins decoded and compared with the original samples,
   with source clock offsets and edge jitter.
"""

import ctypes
import os
import random
import subprocess
import tempfile
import unittest

import pio_sim
import ultranet_ref as U

HERE = os.path.dirname(os.path.abspath(__file__))
FW = os.path.join(HERE, "..", "firmware", "rp2350", "src")
PIO_FILE = os.path.join(FW, "ultranet.pio")
SYS_HZ = 196.5e6

# --------------------------------------------------------------------------
# Build the firmware core as a shared library
# --------------------------------------------------------------------------

_lib = None


def core():
    global _lib
    if _lib is None:
        out = os.path.join(tempfile.mkdtemp(prefix="ultranet_core_"), "libultranet_core.so")
        subprocess.check_call(["cc", "-O2", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
                               "-o", out, os.path.join(FW, "ultranet_core.c")])
        _lib = ctypes.CDLL(out)
        _lib.un_stream_sizeof.restype = ctypes.c_size_t
        _lib.un_stream_push.restype = ctypes.c_bool
        _lib.un_stream_push.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        _lib.un_stream_init.argtypes = [ctypes.c_void_p]
        _lib.un_pack_i2s8.argtypes = [ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)]
    return _lib


class Stream:
    """ctypes wrapper around un_stream_t (field layout mirrors the header)."""

    FIELDS = ["cur"] * 8 + ["frame"] * 8

    def __init__(self):
        lib = core()
        self.buf = ctypes.create_string_buffer(lib.un_stream_sizeof())
        lib.un_stream_init(self.buf)

    def push(self, w: int) -> bool:
        return bool(core().un_stream_push(self.buf, w & 0xFFFFFFFF))

    def _u32(self, idx: int) -> int:
        return int.from_bytes(self.buf.raw[idx * 4: idx * 4 + 4], "little")

    def frame(self):
        return [self._u32(8 + i) for i in range(8)]

    def counters(self):
        # after cur[8], frame[8], have_mask(u8), prev_ch(i8) + 2 pad -> offset 68
        base = 68
        names = ["words", "frames", "code_err", "parity_err", "order_err",
                 "incomplete", "z_ch1", "z_other"]
        raw = self.buf.raw
        return {n: int.from_bytes(raw[base + 4 * i: base + 4 * i + 4], "little")
                for i, n in enumerate(names)}


def pack_i2s8(ch16):
    arr = (ctypes.c_uint32 * 16)(*[c & 0xFFFFFFFF for c in ch16])
    out = (ctypes.c_uint32 * 32)()
    core().un_pack_i2s8(arr, out)
    return list(out)


def lj(audio22: int) -> int:
    return (audio22 << 10) & 0xFFFFFFFF


# --------------------------------------------------------------------------
# I2S pin-level decoder (for the emulated output)
# --------------------------------------------------------------------------

def decode_i2s8(bits):
    """bits: list of (lrclk, sd_byte) captured on BCLK rising edges.
    Returns list of 16-channel frames (left-justified 32-bit)."""
    frames = []
    for i in range(1, len(bits) - 65):
        if bits[i][0] == 0 and bits[i - 1][0] == 1:
            ch = [0] * 16
            for k in range(8):
                left = right = 0
                for b in range(32):
                    left = (left << 1) | ((bits[i + 1 + b][1] >> k) & 1)
                    right = (right << 1) | ((bits[i + 33 + b][1] >> k) & 1)
                ch[2 * k], ch[2 * k + 1] = left, right
            frames.append(ch)
    return frames


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

class TestReferenceModel(unittest.TestCase):
    def test_selftest(self):
        self.assertEqual(U.main(["selftest"]), 0)

    def test_pio_word_roundtrip(self):
        for sf in U.encode_subframes(U.random_frames(50, 1)):
            self.assertEqual(U.Subframe.from_pio_word(sf.to_pio_word()), sf)

    def test_channel_from_tag_and_preamble(self):
        sfs = U.encode_subframes(U.random_frames(200, 2))
        for i, sf in enumerate(sfs):
            self.assertEqual(sf.channel, i % 8)
            self.assertTrue(sf.parity_ok)
        zs = [i for i, sf in enumerate(sfs) if sf.pre == "Z"]
        self.assertEqual(zs[:3], [0, 384, 768])


class TestFirmwareCore(unittest.TestCase):
    def test_demux_matches_reference(self):
        frames = U.random_frames(300, 3)
        s = Stream()
        got = []
        for sf in U.encode_subframes(frames):
            if s.push(sf.to_pio_word()):
                got.append(s.frame())
        self.assertEqual(got, [[lj(v) for v in fr] for fr in frames])
        c = s.counters()
        self.assertEqual(c["frames"], 300)
        self.assertEqual(c["parity_err"] + c["code_err"] + c["order_err"] + c["incomplete"], 0)
        self.assertEqual(c["z_other"], 0)
        self.assertGreater(c["z_ch1"], 0)

    def test_detects_errors(self):
        s = Stream()
        sfs = U.encode_subframes(U.random_frames(4, 4))
        words = [sf.to_pio_word() for sf in sfs]
        words[3] ^= 1 << 20                       # parity error
        words[9] = (words[9] & ~0xF) | 0x3        # bad preamble code
        del words[17]                             # dropped subframe
        for w in words:
            s.push(w)
        c = s.counters()
        self.assertEqual(c["parity_err"], 1)
        self.assertEqual(c["code_err"], 1)
        self.assertGreaterEqual(c["order_err"], 1)

    def test_rotation_is_impossible(self):
        """The bug seen with counter-based demuxers (ch3-8 showing up as 1-6)
        cannot happen: start decoding at any subframe, channels stay put."""
        frames = U.random_frames(20, 5)
        words = [sf.to_pio_word() for sf in U.encode_subframes(frames)]
        for start in range(8):
            s = Stream()
            got = []
            for w in words[start:]:
                if s.push(w):
                    got.append(s.frame())
            self.assertEqual(got[1:], [[lj(v) for v in fr] for fr in frames[1:]])

    def test_i2s_pack(self):
        rng = random.Random(9)
        ch = [lj(rng.randint(U.AUDIO_MIN, U.AUDIO_MAX)) for _ in range(16)]
        words = pack_i2s8(ch)
        bits = []
        for _ in range(4):                         # identical frames back-to-back
            for w in words:
                for h in (w & 0xFFFF, w >> 16):
                    bits.append(((h >> 8) & 1, h & 0xFF))
        dec = decode_i2s8(bits)
        self.assertGreaterEqual(len(dec), 2)
        self.assertEqual(dec[0], ch)


def run_e2e(n_frames=26, ppm=0.0, jitter_ns=0.0, invert=False, b_offset_ui=23.4, seed=11):
    """Run the cycle-level co-simulation.  Returns (decoded_frames, frames_a, frames_b, stats)."""
    progs = pio_sim.assemble(PIO_FILE)
    fa = U.random_frames(n_frames, seed)
    fb = U.random_frames(n_frames, seed + 1)
    rng = random.Random(seed)
    sim = pio_sim.System()
    ui_s = U.UI_SECONDS * (1 + ppm * 1e-6)
    t_start = 0.37e-6                                # decoder starts mid-stream
    for pin, frames, off in ((0, fa, 0.0), (1, fb, b_offset_ui * ui_s)):
        ui = U.subframes_to_ui(U.encode_subframes(frames), 1 if invert else 0)
        init, edges = U.ui_to_edges(ui, ui_s, jitter_ns * 1e-9, rng, t0=-t_start + off)
        sig = U.EdgeSignal(init, edges)
        sim.gpio.inputs[pin] = (lambda c, s=sig: s.level_at(c / SYS_HZ))

    p0, p1 = sim.blocks[0], sim.blocks[1]
    rx = progs["ultranet_rx"]
    sms = []
    for pin in (0, 1):
        cfg = pio_sim.SMConfig(in_base=pin, jmp_pin=pin, in_shift_right=True,
                               autopush=False, fifo_depth_rx=8)
        sm = p0.add_sm(rx, cfg, entry="eh")
        sm.x, sm.y, sm.osr = 0x1F, 0x6, 0x9
        sms.append(sm)
    ocfg = pio_sim.SMConfig(out_base=2, out_count=9, sideset_base=11, out_shift_right=True,
                            autopull=True, pull_thresh=32, fifo_depth_tx=8,
                            clkdiv_int=3, clkdiv_frac=226)
    smo = p1.add_sm(progs["i2s8_out"], ocfg)

    sa, sb = Stream(), Stream()
    outq = []
    for _ in range(2):
        outq.extend(pack_i2s8([0] * 16))             # prefill: 2 silent frames
    bits = []
    last_bclk = 0
    last_mclk = 0
    mclk_edges = 0
    total_cycles = int(n_frames / U.FS * SYS_HZ) - int(t_start * SYS_HZ) - 2000
    for _ in range(total_cycles):
        sim.step()
        # --- CPU model ---
        while sms[1].rx:
            sb.push(sms[1].rx.popleft())
        while sms[0].rx:
            if sa.push(sms[0].rx.popleft()):
                outq.extend(pack_i2s8(sa.frame() + sb.frame()))
        while outq and len(smo.tx) < 8:
            smo.tx.append(outq.pop(0))
        # --- capture I2S on BCLK rising edges ---
        bclk = sim.gpio.out.get(11, 0)
        if bclk and not last_bclk:
            sd = sum(sim.gpio.out.get(2 + k, 0) << k for k in range(8))
            bits.append((sim.gpio.out.get(10, 0), sd))
        last_bclk = bclk
        mclk = sim.gpio.out.get(12, 0)
        if mclk and not last_mclk:
            mclk_edges += 1
        last_mclk = mclk
    stats = {"A": sa.counters(), "B": sb.counters(), "bclks": len(bits), "mclks": mclk_edges,
             "out_backlog_words": len(outq) + len(smo.tx)}
    return decode_i2s8(bits), fa, fb, stats


class TestEndToEnd(unittest.TestCase):
    def check(self, **kw):
        dec, fa, fb, st = run_e2e(**kw)
        exp_a = [[lj(v) for v in fr] for fr in fa]
        exp_b = [[lj(v) for v in fr] for fr in fb]
        # find the first output frame carrying real stream-A data
        start = next(i for i, fr in enumerate(dec) if fr[:8] in exp_a)
        n0 = exp_a.index(dec[start][:8])
        b_lag = next(d for d in (0, 1, -1) if dec[start + 1][8:] == exp_b[n0 + 1 - d])
        good = 0
        for j, fr in enumerate(dec[start + 1:], start=1):
            n = n0 + j
            if n >= len(fa):
                break
            self.assertEqual(fr[:8], exp_a[n], f"stream A frame {n}")
            self.assertEqual(fr[8:], exp_b[n - b_lag], f"stream B frame {n}")
            good += 1
        self.assertGreater(good, len(fa) - 8, st)
        for s in ("A", "B"):
            c = st[s]
            # words before lock may be garbage; after lock, no errors
            self.assertLessEqual(c["parity_err"] + c["code_err"], 2, st)
        # output stays locked: backlog never grows
        self.assertLessEqual(st["out_backlog_words"], 3 * 32, st)
        # MCLK is exactly 4 x BCLK = 256 fs
        self.assertLessEqual(abs(st["mclks"] - 4 * st["bclks"]), 4, st)
        return st

    def test_nominal(self):
        self.check()

    def test_fast_source_plus_jitter(self):
        self.check(ppm=+300, jitter_ns=1.5, seed=21)

    def test_slow_source_plus_jitter(self):
        self.check(ppm=-300, jitter_ns=1.5, seed=31, b_offset_ui=411.0)

    def test_wrong_polarity_gives_no_valid_frames(self):
        dec, fa, fb, st = run_e2e(n_frames=8, invert=True)
        self.assertEqual(st["A"]["frames"] - st["A"]["incomplete"], 0, st)


if __name__ == "__main__":
    unittest.main()
