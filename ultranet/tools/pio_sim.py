#!/usr/bin/env python3
"""
Minimal cycle-level RP2040/RP2350 PIO emulator - just enough of the
instruction set to run ultranet/firmware/rp2350/src/ultranet.pio against
synthetic Ultranet signals.

Modelled:
  * wait (pin / gpio / irq incl. RP2350 prev/next), jmp (all conditions),
    in, out, push, pull, mov, irq, set, nop
  * delays, side-set (with opt), .wrap / .wrap_target, .define expressions
  * shift direction, autopush / autopull with thresholds, FIFO depth/join
  * 2-flop input synchroniser (inputs seen 2 cycles late)
  * fractional clock divider (int + frac/256)
  * IRQ flags per PIO block, cross-block prev/next

Not modelled: status/mov-status, exec, pindirs effects, FIFO-mode extras.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------
# Assembler (subset of pioasm syntax)
# --------------------------------------------------------------------------


@dataclass
class Instr:
    op: str
    args: List[str]
    delay: int
    side: Optional[int]
    text: str


@dataclass
class Program:
    name: str
    instrs: List[Instr] = field(default_factory=list)
    labels: Dict[str, int] = field(default_factory=dict)
    public: Dict[str, int] = field(default_factory=dict)
    wrap_target: int = 0
    wrap: Optional[int] = None
    sideset_bits: int = 0
    sideset_opt: bool = False
    defines: Dict[str, int] = field(default_factory=dict)


def _eval(expr: str, env: Dict[str, int]) -> int:
    expr = expr.strip()
    if not re.fullmatch(r"[\w\s+\-*/()%<>&|^~]+", expr):
        raise ValueError(f"bad expression {expr!r}")
    py = re.sub(r"(?<!/)/(?!/)", "//", expr)
    return int(eval(py, {"__builtins__": {}}, dict(env)))  # noqa: S307 - restricted


def assemble(path: str) -> Dict[str, Program]:
    progs: Dict[str, Program] = {}
    global_defs: Dict[str, int] = {}
    cur: Optional[Program] = None
    pending: List[Tuple[Program, Instr, List[str]]] = []
    with open(path) as fh:
        source = fh.readlines()
    for raw in source:
        line = raw.split(";")[0].split("//")[0].strip()
        if not line:
            continue
        if line.startswith("."):
            parts = line.split()
            d = parts[0]
            if d == ".program":
                cur = Program(parts[1], defines=dict(global_defs))
                progs[cur.name] = cur
            elif d == ".define":
                rest = parts[1:]
                if rest[0].upper() == "PUBLIC":
                    rest = rest[1:]
                name, expr = rest[0], " ".join(rest[1:])
                tgt = cur.defines if cur else global_defs
                tgt[name] = _eval(expr, (cur.defines if cur else global_defs))
            elif d == ".side_set":
                cur.sideset_bits = int(parts[1])
                cur.sideset_opt = "opt" in parts[2:]
            elif d == ".wrap_target":
                cur.wrap_target = len(cur.instrs)
            elif d == ".wrap":
                cur.wrap = len(cur.instrs) - 1
            # .pio_version, .origin, .lang_opt ... ignored
            continue
        m = re.match(r"^(public\s+)?(\w+):\s*(.*)$", line)
        if m:
            cur.labels[m.group(2)] = len(cur.instrs)
            if m.group(1):
                cur.public[m.group(2)] = len(cur.instrs)
            line = m.group(3)
            if not line:
                continue
        delay = 0
        dm = re.search(r"\[([^\]]+)\]\s*$", line)
        if dm:
            delay = _eval(dm.group(1), cur.defines)
            line = line[: dm.start()].strip()
        side = None
        sm_ = re.search(r"\bside\s+(\S+)\s*$", line)
        if sm_:
            side = _eval(sm_.group(1), cur.defines)
            line = line[: sm_.start()].strip()
        toks = line.replace(",", " ").split()
        ins = Instr(toks[0].lower(), toks[1:], delay, side, raw.strip())
        maxd = (1 << (5 - cur.sideset_bits - (1 if cur.sideset_opt else 0))) - 1
        if delay > maxd:
            raise ValueError(f"delay {delay} > {maxd}: {raw.strip()}")
        cur.instrs.append(ins)
    for p in progs.values():
        if p.wrap is None:
            p.wrap = len(p.instrs) - 1
        if len(p.instrs) > 32:
            raise ValueError(f"{p.name}: {len(p.instrs)} instructions > 32")
    return progs


# --------------------------------------------------------------------------
# Emulator
# --------------------------------------------------------------------------


class GPIO:
    """Shared pin state.  Inputs are callables of the sys-cycle number."""

    def __init__(self):
        self.out: Dict[int, int] = {}
        self.inputs: Dict[int, Callable[[int], int]] = {}
        self.cycle = 0

    def raw(self, pin: int, cycle: int) -> int:
        f = self.inputs.get(pin)
        if f is not None:
            return f(cycle)
        return self.out.get(pin, 0)

    def read(self, pin: int) -> int:
        # 2-flop synchroniser: the SM sees the value from 2 cycles ago
        return self.raw(pin, self.cycle - 2)


@dataclass
class SMConfig:
    in_base: int = 0
    jmp_pin: int = 0
    out_base: int = 0
    out_count: int = 0
    set_base: int = 0
    set_count: int = 0
    sideset_base: int = 0
    in_shift_right: bool = True
    autopush: bool = False
    push_thresh: int = 32
    out_shift_right: bool = True
    autopull: bool = False
    pull_thresh: int = 32
    fifo_depth_rx: int = 4
    fifo_depth_tx: int = 4
    clkdiv_int: int = 1
    clkdiv_frac: int = 0


class PIOBlock:
    def __init__(self, gpio: GPIO, index: int):
        self.gpio = gpio
        self.index = index
        self.irq = [0] * 8
        self.sms: List["SM"] = []
        self.prev: Optional["PIOBlock"] = None
        self.next: Optional["PIOBlock"] = None

    def add_sm(self, prog: Program, cfg: SMConfig, entry: Optional[str] = None) -> "SM":
        sm = SM(self, len(self.sms), prog, cfg)
        if entry:
            sm.pc = prog.labels[entry]
        else:
            sm.pc = prog.wrap_target if prog.wrap_target else 0
        self.sms.append(sm)
        return sm


class SM:
    def __init__(self, block: PIOBlock, num: int, prog: Program, cfg: SMConfig):
        self.block, self.num, self.prog, self.cfg = block, num, prog, cfg
        self.pc = 0
        self.x = self.y = 0
        self.isr = self.osr = 0
        self.isr_count = 0
        self.osr_count = 32          # empty
        self.delay = 0
        self.rx: deque = deque()
        self.tx: deque = deque()
        self.enabled = True
        self._acc = 0
        self.stall_cycles = 0

    # ---- helpers ----
    @property
    def gpio(self) -> GPIO:
        return self.block.gpio

    def _irq_target(self, args: List[str]) -> Tuple[PIOBlock, int]:
        blk = self.block
        a = [x.lower() for x in args]
        if "prev" in a:
            blk = blk.prev
            a.remove("prev")
        if "next" in a:
            blk = blk.next
            a.remove("next")
        rel = "rel" in a
        nums = [x for x in a if x not in ("rel", "set", "nowait", "wait", "clear")]
        n = _eval(nums[0], self.prog.defines)
        if rel:
            n = (n & 4) | ((n + self.num) & 3)
        return blk, n

    def _src(self, s: str, n: int = 32) -> int:
        s = s.lower()
        if s == "pins":
            v = 0
            for i in range(n):
                v |= self.gpio.read(self.cfg.in_base + i) << i
            return v
        return {"x": self.x, "y": self.y, "null": 0, "isr": self.isr, "osr": self.osr}[s]

    def _push(self, block: bool) -> bool:
        if len(self.rx) >= self.cfg.fifo_depth_rx:
            if block:
                return False
            self.isr, self.isr_count = 0, 0       # noblock: data lost
            return True
        self.rx.append(self.isr & 0xFFFFFFFF)
        self.isr, self.isr_count = 0, 0
        return True

    def _pull(self, block: bool) -> bool:
        if not self.tx:
            if block:
                return False
            self.osr, self.osr_count = self.x, 0
            return True
        self.osr, self.osr_count = self.tx.popleft(), 0
        return True

    def _set_pins(self, base: int, count: int, value: int):
        for i in range(count):
            self.gpio.out[base + i] = (value >> i) & 1

    # ---- one SM clock ----
    def tick(self) -> None:
        if not self.enabled:
            return
        # fractional divider
        div256 = self.cfg.clkdiv_int * 256 + self.cfg.clkdiv_frac
        self._acc += 256
        if self._acc < div256:
            return
        self._acc -= div256

        if self.delay:
            self.delay -= 1
            return
        ins = self.prog.instrs[self.pc]
        if ins.side is not None and self.prog.sideset_bits:
            nbits = self.prog.sideset_bits - (1 if self.prog.sideset_opt else 0)
            self._set_pins(self.cfg.sideset_base, nbits, ins.side)
        done, jump = self._exec(ins)
        if not done:
            self.stall_cycles += 1
            return
        if jump is not None:
            self.pc = jump
        elif self.pc == self.prog.wrap:
            self.pc = self.prog.wrap_target
        else:
            self.pc += 1
        self.delay = ins.delay

    def _exec(self, ins: Instr) -> Tuple[bool, Optional[int]]:
        op, a = ins.op, ins.args
        if op == "nop":
            return True, None
        if op == "wait":
            pol = int(a[0])
            kind = a[1].lower()
            if kind in ("pin", "gpio"):
                n = _eval(a[2], self.prog.defines)
                pin = self.cfg.in_base + n if kind == "pin" else n
                return self.gpio.read(pin) == pol, None
            if kind == "irq":
                blk, n = self._irq_target(a[2:])
                if blk.irq[n] == pol:
                    if pol == 1:
                        blk.irq[n] = 0
                    return True, None
                return False, None
            raise NotImplementedError(ins.text)
        if op == "jmp":
            if len(a) == 1:
                return True, self.prog.labels[a[0]]
            cond, tgt = a[0].lower(), self.prog.labels[a[1]]
            take = False
            if cond == "!x":
                take = self.x == 0
            elif cond == "x--":
                take = self.x != 0
                self.x = (self.x - 1) & 0xFFFFFFFF
            elif cond == "!y":
                take = self.y == 0
            elif cond == "y--":
                take = self.y != 0
                self.y = (self.y - 1) & 0xFFFFFFFF
            elif cond == "x!=y":
                take = self.x != self.y
            elif cond == "pin":
                take = self.gpio.read(self.cfg.jmp_pin) == 1
            elif cond == "!osre":
                take = self.osr_count < self.cfg.pull_thresh
            else:
                raise NotImplementedError(ins.text)
            return True, (tgt if take else None)
        if op == "in":
            n = _eval(a[1], self.prog.defines) or 32
            if self.cfg.autopush and self.isr_count >= self.cfg.push_thresh:
                if not self._push(True):
                    return False, None
            data = self._src(a[0], n) & ((1 << n) - 1)
            if self.cfg.in_shift_right:
                self.isr = ((self.isr >> n) | (data << (32 - n))) & 0xFFFFFFFF if n < 32 else data
            else:
                self.isr = ((self.isr << n) | data) & 0xFFFFFFFF
            self.isr_count = min(32, self.isr_count + n)
            if self.cfg.autopush and self.isr_count >= self.cfg.push_thresh:
                self._push(False)
            return True, None
        if op == "out":
            n = _eval(a[1], self.prog.defines) or 32
            if self.cfg.autopull and self.osr_count >= self.cfg.pull_thresh:
                if not self._pull(True):
                    return False, None
            if self.cfg.out_shift_right:
                data = self.osr & ((1 << n) - 1)
                self.osr = (self.osr >> n) if n < 32 else 0
            else:
                data = (self.osr >> (32 - n)) & ((1 << n) - 1)
                self.osr = (self.osr << n) & 0xFFFFFFFF
            self.osr_count = min(32, self.osr_count + n)
            dst = a[0].lower()
            if dst == "pins":
                self._set_pins(self.cfg.out_base, min(n, self.cfg.out_count), data)
            elif dst == "x":
                self.x = data
            elif dst == "y":
                self.y = data
            elif dst == "null":
                pass
            else:
                raise NotImplementedError(ins.text)
            return True, None
        if op == "push":
            flags = [x.lower() for x in a]
            if "iffull" in flags and self.isr_count < self.cfg.push_thresh:
                return True, None
            return self._push("noblock" not in flags), None
        if op == "pull":
            flags = [x.lower() for x in a]
            if "ifempty" in flags and self.osr_count < self.cfg.pull_thresh:
                return True, None
            return self._pull("noblock" not in flags), None
        if op == "set":
            v = _eval(a[1], self.prog.defines)
            dst = a[0].lower()
            if dst == "x":
                self.x = v
            elif dst == "y":
                self.y = v
            elif dst == "pins":
                self._set_pins(self.cfg.set_base, self.cfg.set_count, v)
            else:
                raise NotImplementedError(ins.text)
            return True, None
        if op == "irq":
            args = [x.lower() for x in a]
            blk, n = self._irq_target(args)
            if "clear" in args:
                blk.irq[n] = 0
                return True, None
            blk.irq[n] = 1
            if "wait" in args:
                return False, None   # simplified: not used here
            return True, None
        if op == "mov":
            dst, src = a[0].lower(), a[1].lower()
            inv = src.startswith(("!", "~"))
            src = src.lstrip("!~")
            v = self._src(src)
            if inv:
                v = ~v & 0xFFFFFFFF
            if dst == "x":
                self.x = v
            elif dst == "y":
                self.y = v
            elif dst == "isr":
                self.isr, self.isr_count = v, 0
            elif dst == "osr":
                self.osr, self.osr_count = v, 0
            else:
                raise NotImplementedError(ins.text)
            return True, None
        raise NotImplementedError(ins.text)


class System:
    def __init__(self, nblocks: int = 3):
        self.gpio = GPIO()
        self.blocks = [PIOBlock(self.gpio, i) for i in range(nblocks)]
        for i, b in enumerate(self.blocks):
            b.prev = self.blocks[(i - 1) % nblocks]
            b.next = self.blocks[(i + 1) % nblocks]
        self.cycle = 0

    def step(self) -> None:
        self.gpio.cycle = self.cycle
        for b in self.blocks:
            for sm in b.sms:
                sm.tick()
        self.cycle += 1
