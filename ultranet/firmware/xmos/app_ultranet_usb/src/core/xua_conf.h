// Device configuration for the Ultranet USB bridge (overrides lib_xua defaults).
// Values given on the compiler command line (CMakeLists.txt) take precedence.
#ifndef _XUA_CONF_H_
#define _XUA_CONF_H_

#include "../../../shared/version.h"

/* Tiles: USB on tile 0, audio I/O on tile 1 (as on XMOS reference designs) */
#define XUA_XUD_TILE_NUM        (0)
#define XUA_AUDIO_IO_TILE_NUM   (1)

/* Everything we do not need is off */
#ifndef MIDI
#define MIDI                    (0)
#endif
#ifndef MIXER
#define MIXER                   (0)
#endif
#define XUA_SPDIF_TX_EN         (0)
#define XUA_SPDIF_RX_EN         (0)
#define XUA_ADAT_TX_EN          (0)
#define XUA_ADAT_RX_EN          (0)
#define XUA_DFU_EN              (1)     /* firmware updates over USB */

/* Clocking: the RP2350 is the I2S master.  MCLK (12.288 MHz = 256 fs) is
 * supplied by the RP2350 too and is phase-locked to BCLK/LRCLK. */
#ifndef CODEC_MASTER
#define CODEC_MASTER            (1)
#endif
#define MCLK_441                (256 * 44100)   /* unused, 48 kHz only */
#define MCLK_48                 (256 * 48000)

#ifndef MIN_FREQ
#define MIN_FREQ                (48000)
#endif
#ifndef MAX_FREQ
#define MAX_FREQ                (48000)
#endif

/* Channel counts (normally set in CMakeLists.txt) */
#ifndef NUM_USB_CHAN_IN
#define NUM_USB_CHAN_IN         (16)
#endif
#ifndef I2S_CHANS_ADC
#define I2S_CHANS_ADC           (16)
#endif
#ifndef NUM_USB_CHAN_OUT
#define NUM_USB_CHAN_OUT        (0)
#endif
#ifndef I2S_CHANS_DAC
#define I2S_CHANS_DAC           (0)
#endif

/* USB identity.  0x20B1 is the XMOS vendor ID, fine for personal use; get
 * your own VID/PID before distributing devices. */
#define VENDOR_ID               (0x20B1)
#define PID_AUDIO_2             (0x0018)
#define PID_AUDIO_1             (0x0019)
#define VENDOR_STR              "DIY"
#define PRODUCT_STR_A2          "Ultranet 16"
#define PRODUCT_STR_A1          "Ultranet 16 (UAC1)"

/* Bus powered from the Mac; the RP2350 board can be powered from the same 5 V */
#define XUA_POWERMODE           XUA_POWERMODE_BUS

#endif
