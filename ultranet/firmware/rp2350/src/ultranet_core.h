// Hardware-independent part of the Ultranet -> I2S firmware.
// Builds for the RP2350 and on a desktop (for the tests in ultranet/tools).
#pragma once
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

// Preamble codes placed in bits [3:0] by the PIO decoder (see ultranet.pio)
#define UN_CODE_X 0x9u
#define UN_CODE_Y 0x6u
#define UN_CODE_Z 0xFu

#define UN_CH_PER_STREAM 8
#define UN_I2S_WORDS_PER_FRAME 32       // 64 bit-times, 2 per 32-bit word

typedef struct {
    uint32_t cur[UN_CH_PER_STREAM];     // frame being assembled (left-justified 32-bit)
    uint32_t frame[UN_CH_PER_STREAM];   // last complete frame
    uint8_t  have_mask;                 // channels received in the current frame
    int8_t   prev_ch;
    // statistics (free-running counters; the caller takes differences)
    uint32_t words;
    uint32_t frames;
    uint32_t code_err;                  // bits[3:0] not a valid preamble code
    uint32_t parity_err;
    uint32_t order_err;                 // channel != previous + 1
    uint32_t incomplete;                // frame closed with channels missing
    uint32_t z_ch1;                     // Z preamble on channel 1 (expected)
    uint32_t z_other;                   // Z preamble elsewhere (unexpected)
    uint32_t peak[UN_CH_PER_STREAM];    // max |sample| since un_stream_take_peaks
} un_stream_t;

void un_stream_init(un_stream_t *s);

// Feed one PIO word.  Returns true when channel 8 (the last of a 48 kHz
// frame) arrived; s->frame then holds the new frame.
bool un_stream_push(un_stream_t *s, uint32_t word);

// Decode helpers (also used by the tests)
static inline bool un_word_valid_code(uint32_t w) {
    uint32_t c = w & 0xFu;
    return c == UN_CODE_X || c == UN_CODE_Y || c == UN_CODE_Z;
}
static inline unsigned un_word_channel(uint32_t w) {        // 0..7
    return (((w >> 4) & 3u) << 1) | ((w & 0xFu) == UN_CODE_Y ? 1u : 0u);
}
static inline uint32_t un_word_sample(uint32_t w) {         // 22-bit audio, left-justified
    return (w << 4) & 0xFFFFFC00u;
}

// Pack 16 left-justified channels (ch[0..15]) into one 48 kHz I2S frame for
// the 8-line i2s8_out PIO program.  Line k carries ch[2k] (left) and
// ch[2k+1] (right).  Output halfword per bit-time: bits[7:0] = SD7..SD0,
// bit[8] = LRCLK.  Standard Philips I2S: LRCLK low = left, data delayed by
// one BCLK after the LRCLK edge, MSB first.
void un_pack_i2s8(const uint32_t ch[16], uint32_t out[UN_I2S_WORDS_PER_FRAME]);

// Peak meter helper: returns peaks (left-justified magnitude) and clears them.
void un_stream_take_peaks(un_stream_t *s, uint32_t peaks[UN_CH_PER_STREAM]);

// For ctypes-based tests
size_t un_stream_sizeof(void);

#ifdef __cplusplus
}
#endif
