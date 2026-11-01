#include "ultranet_core.h"
#include <string.h>

size_t un_stream_sizeof(void) { return sizeof(un_stream_t); }

void un_stream_init(un_stream_t *s) {
    memset(s, 0, sizeof(*s));
    s->prev_ch = -1;
}

static inline uint32_t parity32(uint32_t x) {
#if defined(__GNUC__)
    return (uint32_t)__builtin_parity(x);
#else
    x ^= x >> 16; x ^= x >> 8; x ^= x >> 4; x ^= x >> 2; x ^= x >> 1;
    return x & 1u;
#endif
}

bool un_stream_push(un_stream_t *s, uint32_t w) {
    s->words++;
    if (!un_word_valid_code(w)) {
        s->code_err++;
        s->prev_ch = -1;
        return false;
    }
    if (parity32(w >> 4)) {             // even parity over AES3 slots 4..31
        s->parity_err++;
        return false;                   // keep the previous sample for this channel
    }
    unsigned ch = un_word_channel(w);
    if (s->prev_ch >= 0 && ch != (unsigned)((s->prev_ch + 1) & 7))
        s->order_err++;
    s->prev_ch = (int8_t)ch;

    if ((w & 0xFu) == UN_CODE_Z) {
        if (ch == 0) s->z_ch1++; else s->z_other++;
    }

    if (ch == 0) s->have_mask = 0;      // new frame
    uint32_t smp = un_word_sample(w);
    s->cur[ch] = smp;
    s->have_mask |= (uint8_t)(1u << ch);

    int32_t v = (int32_t)smp;
    uint32_t mag = v < 0 ? (uint32_t)(-(v + 1)) : (uint32_t)v;
    if (mag > s->peak[ch]) s->peak[ch] = mag;

    if (ch == 7) {
        if (s->have_mask != 0xFF) s->incomplete++;
        memcpy(s->frame, s->cur, sizeof(s->frame));
        s->frames++;
        return true;
    }
    return false;
}

void un_stream_take_peaks(un_stream_t *s, uint32_t peaks[UN_CH_PER_STREAM]) {
    memcpy(peaks, s->peak, sizeof(s->peak));
    memset(s->peak, 0, sizeof(s->peak));
}

// spread[b]: the 8 bits of b (MSB first) placed in bit 0 of 8 consecutive
// bytes, so byte j holds bit (7-j) of b.
static uint64_t spread[256];
static bool spread_ready;

static void init_spread(void) {
    for (unsigned b = 0; b < 256; b++) {
        uint64_t v = 0;
        for (unsigned j = 0; j < 8; j++)
            if (b & (0x80u >> j)) v |= (uint64_t)1 << (8 * j);
        spread[b] = v;
    }
    spread_ready = true;
}

void un_pack_i2s8(const uint32_t ch[16], uint32_t out[UN_I2S_WORDS_PER_FRAME]) {
    if (!spread_ready) init_spread();
    // Per line: 64-bit sequence L(31..0) R(31..0), shifted right by one for
    // the I2S one-bit delay (bit-time 0 carries the previous right LSB, which
    // is always 0 because the audio is only 22 bits).
    uint64_t seq[8];
    for (unsigned k = 0; k < 8; k++)
        seq[k] = (((uint64_t)ch[2 * k] << 32) | ch[2 * k + 1]) >> 1;

    uint16_t ht[64];
    for (unsigned g = 0; g < 8; g++) {          // 8 bit-times per group
        unsigned shift = 56 - 8 * g;
        uint64_t acc = 0;
        for (unsigned k = 0; k < 8; k++)
            acc |= spread[(seq[k] >> shift) & 0xFF] << k;
        for (unsigned j = 0; j < 8; j++) {
            unsigned t = 8 * g + j;
            ht[t] = (uint16_t)(((acc >> (8 * j)) & 0xFF) | ((t >= 32) ? 0x100u : 0u));
        }
    }
    for (unsigned i = 0; i < 32; i++)
        out[i] = (uint32_t)ht[2 * i] | ((uint32_t)ht[2 * i + 1] << 16);
}
