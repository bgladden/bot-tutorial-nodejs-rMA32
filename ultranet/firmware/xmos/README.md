# XMOS USB Audio Class 2 bridge (16 in, I2S slave)

This is an overlay for [xmos/sw_usb_audio](https://github.com/xmos/sw_usb_audio),
the XMOS USB audio reference software (built on `lib_xua`). The RP2350
supplies 8 I2S data lines plus LRCLK, BCLK and MCLK, all locked to the
Ultranet source. The XU316 is the I2S slave and shows up on the Mac as a
class-compliant 16-input device.

**Status: configuration only. It has not been built yet (it needs the XMOS XTC
tools) and has not been tested on hardware.** It relies only on features
`sw_usb_audio` already ships in other configs: I2S slave (`2ASi8o8…`),
input-only builds, and up to 8 ADC wires.

## Build

```sh
git clone https://github.com/xmos/sw_usb_audio.git
cp -r ultranet/firmware/xmos/app_ultranet_usb sw_usb_audio/
cd sw_usb_audio/app_ultranet_usb
cmake -G "Unix Makefiles" -B build       # with XTC tools on PATH, XMOS_CMAKE_PATH set
xmake -C build                           # builds config 2ASi16o0
xflash bin/2ASi16o0/app_ultranet_usb_2ASi16o0.xe
```

## Before you build

1. **Board and port map.** Edit `src/core/ultranet_bridge.xn` so that the
   package, oscillator and port locations match your xcore.ai board. The
   ports you need: tile 1 `1B` (LRCLK), `1C` (BCLK), `1D` (MCLK), `1I`–`1P`
   (8 data lines); tile 0 `1D` (MCLK for the USB-side counter). Wire the
   RP2350 MCLK to both tiles' 1D pins.
2. **I/O voltage.** The RP2350 GPIOs are 3.3 V. Check the xcore I/O supply on
   your board; some boards run banks at 1.8 V and need level shifters.
3. **USB IDs.** `xua_conf.h` uses the XMOS VID with a demo PID. That's fine on
   your own desk. Get your own VID/PID before you distribute devices.

## Settings

| Define | Value | Why |
|---|---|---|
| `CODEC_MASTER` | 1 | RP2350 drives all clocks (locked to the console) |
| `NUM_USB_CHAN_IN` / `I2S_CHANS_ADC` | 16 / 16 | 8 I2S wires × 2 channels |
| `NUM_USB_CHAN_OUT` / `I2S_CHANS_DAC` | 0 / 0 | Recording only. Avoids playback-rate feedback |
| `MIN_FREQ` = `MAX_FREQ` | 48000 | Ultranet is 48 kHz |
| `MCLK_48` | 256 × 48000 | RP2350 supplies 256 fs |
| `XUA_I2S_N_BITS` | 32 | 32-bit slots |
