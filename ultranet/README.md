# Ultranet → USB: a 16-channel recorder input for the Mac

A DIY receiver for **Behringer Ultranet**, the 16 × 48 kHz digital monitor
bus on the X32, Wing and P16 gear. It shows up on a Mac as a class-compliant
**16-input USB audio interface**.

```
Ultranet (RJ45) ─► RS-422 rx ─► Pico 2 (RP2350 PIO decode) ─8× I2S─► XMOS XU316 ─USB 2.0 HS─► Mac
```

| | |
|---|---|
| [docs/PROTOCOL.md](docs/PROTOCOL.md) | What is known about Ultranet: sources, confidence levels, open questions |
| [docs/DEVICE_DESIGN.md](docs/DEVICE_DESIGN.md) | Why this architecture, schematic notes, pin maps, BOM, clocking, bring-up |
| [tools/ultranet_ref.py](tools/ultranet_ref.py) | Reference encoder/decoder. Also **analyses logic-analyzer captures** and writes WAV |
| [tools/pio_sim.py](tools/pio_sim.py) | Small cycle-level PIO emulator used to test the PIO code |
| [tools/test_ultranet.py](tools/test_ultranet.py) | Tests: model, firmware core, and full pin-to-pin co-simulation |
| [firmware/rp2350](firmware/rp2350) | Pico 2 firmware (pico-sdk) |
| [firmware/xmos](firmware/xmos) | XMOS `sw_usb_audio` overlay (16-in, I2S slave) |

## Key findings

* Each RJ45 pair (1/2 = ch 1–8, 3/6 = ch 9–16) carries a standard-format AES3
  stream at 192 kHz stereo timing. Its 384 k subframes/s are 8 channels at
  48 kHz, sent round-robin.
* Only **22 bits** are audio. The 2 LSBs are a **channel-pair tag**, which
  together with the X/Y preamble names every subframe's channel. Decoders
  that count subframes instead can end up one pair off (a bug other
  projects have reported).
* Neither the RP2040/RP2350 (USB Full-Speed only) nor a Teensy (stock USB
  audio is stereo) can carry 16 channels to a Mac alone. So the design splits
  into **RP2350 for decoding** and **XMOS for USB audio**. The I2S clocks are
  derived from the Ultranet stream, so recordings are bit-exact with no
  resampling.

## Status

* Protocol model, PIO programs and firmware data path: **verified in
  simulation**. Bit-exact over ±2000 ppm clock offset; tolerates edge jitter
  up to ~3 ns RMS (fails at 5 ns).
* RP2350 firmware compiles cleanly for the Pico 2. **It has not run on
  hardware yet.**
* XMOS configuration is written but not built (needs the XMOS tools).

## Quick start

```sh
cd ultranet/tools
python3 -m unittest -v test_ultranet          # model + firmware core + co-simulation
python3 ultranet_ref.py selftest
python3 ultranet_ref.py analyze my_capture.csv --wav ch1-8.wav   # real captures
```
