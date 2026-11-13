# DeskHop: Magic Trackpad passthrough (experimental)

A patch for [hrvach/deskhop](https://github.com/hrvach/deskhop) (made against commit `7c1e7b2`) that lets a
cabled Apple Magic Trackpad be shared between two Macs with native macOS gestures.

**Status: compiles, never run on hardware.** It was written without a DeskHop or a trackpad to test on.
Expect to iterate; the debug build exists for that.

## How it works

1. A Magic Trackpad (USB IDs `05ac:0265` Lightning or `05ac:0324` USB-C) is plugged into either DeskHop port.
2. The board reads the trackpad's USB identity: device descriptor, name and serial strings, every HID
   interface and its report descriptor. It sends a copy to the other board.
3. Both boards drop off USB for half a second and come back *as that trackpad* (same IDs, same interfaces
   and endpoint addresses), with DeskHop's keyboard and mouse interfaces added after the trackpad's.
4. Raw trackpad reports go, unmodified, only to the selected Mac.
5. When a Mac's driver reads or writes a HID report (this is how macOS switches the trackpad into
   multitouch mode), the request is relayed to the real trackpad and the real answer is returned.
6. On switching, the Mac you leave is told all fingers lifted, and the trackpad is put back into whatever
   state the Mac you arrive at last asked for.

## Files

| File | What it is |
|---|---|
| `magic-trackpad-passthrough.patch` | The source change. Apply with `git apply` in a deskhop checkout. |
| `deskhop-trackpad.uf2` | Normal build. Switch Macs with the keyboard hotkey. |
| `deskhop-trackpad-debug.uf2` | Same, plus a USB serial port that logs what the passthrough is doing. |
| `deskhop-trackpad-edge-switch.uf2` | Edge switching on (`TP_POINTER_MODE 1`), see below. |

Flash both boards with the same file (hold the Pico's button while plugging it in, copy the `.uf2`).

## Two modes

- **Native (default).** Everything is passed through, so pointer feel and every gesture come from macOS.
  DeskHop cannot see where the cursor is in this mode, so you switch Macs with the keyboard hotkey.
- **Edge switching (`TP_POINTER_MODE 1`).** One finger moves DeskHop's own pointer, so pushing past the
  screen edge switches Macs like a mouse does. Two or more fingers are passed through, so scroll, pinch
  and swipes stay native. The cost: one-finger pointer speed, tap-to-click and drag come from DeskHop's
  firmware rather than macOS, and Force Touch with one finger is lost.

Settings are at the bottom of `src/include/user_config.h`.

## Suggested test order

1. Flash `deskhop-trackpad-debug.uf2` to both boards. Connect one Mac, a keyboard, and the trackpad by cable.
2. On the Mac, open System Information > USB. After a second or so the DeskHop should be listed as a
   Magic Trackpad. If it still says "DeskHop Switch", the capture step failed.
3. Open System Settings > Trackpad. If the full gesture pane appears, macOS has accepted the clone.
4. Try two-finger scroll, pinch and a three-finger swipe.
5. Connect the second Mac and switch with the hotkey.
6. Only then try the edge-switch build.

To read the log, open the "DeskHop Debug" serial port (`screen /dev/tty.usbmodem* 115200`). Lines starting
`[tp]` show the capture result and every relayed `GET`/`SET` with its interface, report ID and result.
That log is what is needed to fix whatever does not work.

## Known unknowns

- **Whether macOS binds its multitouch driver to the clone at all.** The clone matches the IDs, strings,
  interface numbers, endpoint addresses and report descriptors, but adds two interfaces of its own.
- **Requests other than HID GET/SET_REPORT.** Vendor-specific control requests are not relayed.
- **Trackpads with non-HID interfaces.** Those interfaces are skipped.
- **Bluetooth auto-pairing.** macOS normally pairs a Magic Trackpad over Bluetooth when it is plugged in by
  cable. Both Macs will see the same serial number and may both try.
- **Two Macs configuring one trackpad.** Both drivers' settings reach the same device. Set
  `TP_FORWARD_INACTIVE_SETS 0` if the unselected Mac disturbs the selected one.
- **Unplugging the trackpad** leaves both boards presenting it until they are power-cycled.
- **Trackpad data format** (12-byte header, 9 bytes per finger) is taken from how Linux handles this
  device; only the edge-switch mode and the "fingers lifted" message depend on it.
- **Power.** The trackpad charges through the DeskHop from the Mac's USB port.

To go back to stock, flash an official DeskHop release to both boards.
