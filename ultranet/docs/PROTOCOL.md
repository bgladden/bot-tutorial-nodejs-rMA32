# Behringer Ultranet: what is known

Behringer has never published Ultranet. This document combines what three
independent open-source projects found, and states how sure we are of each
fact. `tools/ultranet_ref.py` is the executable version of this document.

| Source | What it is | What it contributes |
|---|---|---|
| [xn--nding-jua/UltranetReceiver](https://github.com/xn--nding-jua/UltranetReceiver) (Christian Nöding) | Cyclone 10LP FPGA receiver on an Arduino MKR Vidor 4000, 16 channels | First public decode. Uses a generic AES3 receiver core; counts subframes from the Z preamble. Notes the P16-I drives the line with a 74LVC245. |
| [doughadfield/Ultranet-to-I2S](https://github.com/doughadfield/Ultranet-to-I2S) | RP2040 PIO decoder: 8 channels to 4× I2S DACs | Shows PIO can decode the line at 196.5 MHz. Uses an MC3486 RS-422 receiver. States that the audio is 22 bits. |
| [TheStaticTurtle/HyperNet](https://github.com/TheStaticTurtle/HyperNet) (MIT) | Gowin FPGA stagebox that **transmits and receives** Ultranet; works with Behringer gear | Shows the 2 LSBs are a channel-pair tag. Uses an off-the-shelf DIX9211 AES3 receiver as the front end. Has a channel-status pattern that Behringer receivers accept. |

Other write-ups: [Hackaday article](https://hackaday.com/2024/03/05/reverse-engineering-the-behringer-ultranet-protocol),
[GroupDIY thread](https://groupdiy.com/threads/behringers-ultranet-reversed.86820),
[TheStaticTurtle blog comments](https://github.com/TheStaticTurtle/blog-comments/discussions/44),
and [tuck1s/UltranetReceiver](https://github.com/tuck1s/UltranetReceiver), an XMOS-based SD recorder.

Confidence: **[C]** confirmed by at least two independent working
implementations. **[L]** likely, with one source or an inference from the
others. **[?]** still to be confirmed on a real capture (see "Open questions").

---

## 1. Physical layer

| Item | Value | |
|---|---|---|
| Connector | RJ45, ordinary Cat5e or better | [C] |
| Channels 1–8 | pair on pins **1 / 2** | [C] |
| Channels 9–16 | pair on pins **3 / 6** | [C] |
| Pins 4/5, 7/8 | Not used for audio. **Some Behringer devices put DC on them** (P16-D powering P16-M). Leave them unconnected. | [L] |
| Signalling | Differential, unidirectional. The P16-I drives it from a 74LVC245 (3.3 V CMOS, so ~3.3 V per leg). An RS-422 receiver with 100 Ω termination works (MC3486, AM26LV32). | [C] |
| Line rate | 12.288 Mbit/s data, 24.576 MBd biphase (1 UI = 40.69 ns) | [C] |
| Clock | Embedded (biphase-mark). Both pairs come from the same source clock. | [L] |

Nöding notes that a single leg (Tx+) into a 3.3 V input works on a short cable.
A proper differential receiver costs about $1, so use one anyway.

## 2. Line coding: an AES3 stream at "192 kHz"

Each pair carries a stream that is **bit-for-bit an AES3/S-PDIF stream at
192 kHz stereo timing**. That is why off-the-shelf AES3 receivers such as the
DIX9211 (used by HyperNet) or the AK4114 can lock to it. [C]

```
one subframe = 32 time slots = 64 UI = 2.604 µs

slot:  0  1  2  3 | 4 ............................ 27 | 28 29 30 31
       preamble   | 24-bit sample field, LSB first    |  V  U  C  P
```

* **Biphase-mark**: every slot starts with a transition. A `1` has an extra
  transition in the middle of the slot.
* **Preambles** break that rule so they can be found. They are X/M = `11100010`,
  Y/W = `11100100`, Z/B = `11101000` (in UI; inverted if the line was high).
  As run lengths in UI: X = 3,3,1,1; Y = 3,2,1,2; Z = 3,1,1,3.
* **Even parity** over slots 4–31, so every subframe has an even number of
  transitions. As a result **the preamble always starts at the same line level**
  for a given wiring polarity. The PIO decoder relies on this.
* X and Y alternate as in AES3. Z replaces X every 192 AES3 frames (384
  subframes = 48 Ultranet frames = 1 ms). [C]

## 3. Ultranet framing on top of AES3

AES3 "192 kHz stereo" = 384 000 subframes/s = **8 channels × 48 kHz**. Behringer
sends the 8 channels round-robin: [C]

```
subframe: X/Z   Y    X    Y    X    Y    X    Y    X/Z  Y ...
channel :  1    2    3    4    5    6    7    8    1    2 ...
          '----------- one 48 kHz sample period -----------'
```

### The 24-bit sample field

```
 bit 23                                   2   1   0
 +----------------------------------------+-------+
 |  audio, 22-bit two's complement (MSB)  |  tag  |
 +----------------------------------------+-------+
 tag = channel PAIR: 0 = ch1/2, 1 = ch3/4, 2 = ch5/6, 3 = ch7/8
```

* **Audio resolution is 22 bits.** The two LSBs are not audio. [C] HyperNet's
  receiver and transmitter both do this, and doughadfield independently found
  the audio is "22 bits, not 24".
* **Channel = 2 × tag + (preamble == Y ? 1 : 0).** The odd channel of a pair is
  on the X/Z subframe and the even channel is on Y. [C for the tag, L for the
  X/Y assignment, which follows from AES3 left/right + HyperNet's TX order]

This gives **every subframe its own channel number**, so you don't need to
count. Decoders that count from a sync point can lock one pair off. The
reported "channels 3-8 appear as 1-6, then 1-2 as 7-8" bug is exactly that,
because every X subframe looks like channel 1. The firmware and the reference
model here use the tag, and `test_rotation_is_impossible` checks this.

### V, U, C bits

* V (validity) and U (user): not used for audio. Observed 0. [L]
* C (channel status): a 192-bit AES3 channel-status block per channel, aligned
  to Z. HyperNet transmits a fixed pattern that Behringer receivers accept (see
  `hypernet_tx_channel_status.vhd`). A receiver doesn't need it.
  `ultranet_ref.py analyze --cs` dumps it from a capture. [?]

## 4. Timing numbers

| Quantity | Value |
|---|---|
| Audio sample rate | 48 kHz (no 44.1/96 kHz mode is known) |
| Subframes per second (per pair) | 384 000 |
| Bit rate / baud | 12.288 Mbit/s / 24.576 MBd |
| UI | 40.69 ns |
| Subframe | 2.604 µs |
| Z (block) period | 384 subframes = 1 ms |

## 5. Open questions (capture a real stream to settle them)

1. Is the Z preamble always on channel 1? (Nöding's decoder assumes it.)
   The firmware counts `Z@ch1` / `Z@other` live, so this shows up on the
   serial console.
2. What do the channel-status bits contain, and do they differ between X32,
   Wing and P16-I sources?
3. Do V or U ever carry anything, for example in console "mute" states?
4. Exact line levels and whether the source has transformers. Measure with a
   scope across 100 Ω before trusting any input stage.
5. Are the two pairs exactly frequency-locked? (Assumed. The firmware's
   stream-B handling only needs them within ±1 frame.)

To capture: a logic analyzer at **≥ 100 MS/s** (Saleae Logic Pro, DSLogic
Plus; the cheap 24 MHz FX2 clones are too slow), on the output of the RS-422
receiver. Then run:

```
python3 tools/ultranet_ref.py analyze capture.csv --cs --wav ch1-8.wav
```
