// Ultranet -> 16-channel I2S bridge for Raspberry Pi Pico 2 (RP2350)
//
//   GP0  <- Ultranet pair 1/2  (channels 1-8)   via RS-422 receiver
//   GP1  <- Ultranet pair 3/6  (channels 9-16)  via RS-422 receiver
//   GP2..GP9 -> I2S SD0..SD7  (SDk carries channels 2k+1 / 2k+2)
//   GP10 -> I2S LRCLK (48 kHz)        GP11 -> I2S BCLK (3.072 MHz, 64 fs)
//   GP12 -> MCLK 12.288 MHz (256 fs), phase-locked to BCLK
//   USB  -> CDC serial console with lock status and level meters
//
// The Pico is the I2S clock master; its BCLK/LRCLK are locked to the
// incoming Ultranet stream (see ultranet.pio), so the USB-audio bridge that
// follows (XMOS, I2S slave) delivers bit-exact samples at the console's rate.
//
// Core 0: setup, polarity auto-detect, statistics.   Core 1: data path.

#include <stdio.h>
#include <string.h>
#include <math.h>

#include "pico/stdlib.h"
#include "pico/multicore.h"
#include "hardware/pio.h"
#include "hardware/dma.h"
#include "hardware/clocks.h"
#include "hardware/gpio.h"
#include "hardware/regs/dma.h"

#include "ultranet.pio.h"
#include "ultranet_core.h"

// ---------------------------------------------------------------- pins
#define PIN_UNET_A      0
#define PIN_UNET_B      1
#define PIN_I2S_SD0     2       // SD0..SD7 = GP2..GP9
#define PIN_I2S_LRCLK   10      // must be PIN_I2S_SD0 + 8 (9th "out" pin)
#define PIN_I2S_BCLK    11      // side-set bit 0
#define PIN_MCLK        12      // side-set bit 1 (must be PIN_I2S_BCLK + 1)
#ifndef PICO_DEFAULT_LED_PIN
#define PICO_DEFAULT_LED_PIN 25
#endif

// ---------------------------------------------------------------- clocks
// 8 SM cycles per Ultranet UI: 8 * 24.576 MHz = 196.608 MHz.  196.5 MHz is the
// closest the PLL gets from a 12 MHz crystal (1572 MHz / 4 / 2).  This is a
// mild overclock for the RP2350 (150 MHz nominal) that Pico 2 boards run fine.
#define SYS_KHZ         196500

// ---------------------------------------------------------------- PIO usage
#define PIO_RX          pio0    // SM0 = stream A, SM1 = stream B, SM2 = pacer
#define SM_RX_A         0
#define SM_RX_B         1
#define SM_PACER        2
#define PIO_OUT         pio1    // must be the block after PIO_RX ("irq prev")
#define SM_OUT          0

// ---------------------------------------------------------------- buffers
#define RX_RING_LOG2    14                          // bytes: 16 KiB = 4096 words (~10 ms)
#define RX_RING_WORDS   (1u << (RX_RING_LOG2 - 2))
#define OUT_RING_LOG2   13                          // 8 KiB = 2048 words = 64 frames
#define OUT_RING_WORDS  (1u << (OUT_RING_LOG2 - 2))
#define FRAME_WORDS     UN_I2S_WORDS_PER_FRAME      // 32
#define OUT_TARGET      (8 * FRAME_WORDS)           // ~167 us of latency
#define OUT_HIGH        (12 * FRAME_WORDS)
#define OUT_LOW         (2 * FRAME_WORDS)

#define DMA_ENDLESS     ((DMA_CH0_TRANS_COUNT_MODE_VALUE_ENDLESS << DMA_CH0_TRANS_COUNT_MODE_LSB) | 1u)

static uint32_t rx_ring[2][RX_RING_WORDS] __attribute__((aligned(1u << RX_RING_LOG2)));
static uint32_t out_ring[OUT_RING_WORDS]  __attribute__((aligned(1u << OUT_RING_LOG2)));

static int dma_rx[2], dma_out;
static un_stream_t st[2];

// shared between cores (written by core 1)
static volatile uint32_t last_frame_us[2];
static volatile uint32_t out_dups, out_drops, out_resyncs;
static volatile bool pacer_on;

static uint pacer_offset;

// ======================================================================
// PIO setup
// ======================================================================

static void rx_sm_init(uint sm, uint offset, uint pin) {
    PIO pio = PIO_RX;
    pio_gpio_init(pio, pin);                    // function = PIO, clears pad isolation
    gpio_disable_pulls(pin);
    gpio_set_input_hysteresis_enabled(pin, true);

    pio_sm_config c = ultranet_rx_program_get_default_config(offset);
    sm_config_set_in_pins(&c, pin);
    sm_config_set_jmp_pin(&c, pin);
    sm_config_set_in_shift(&c, true /*right*/, false /*no autopush*/, 32);
    sm_config_set_clkdiv_int_frac8(&c, 1, 0);
    pio_sm_init(pio, sm, offset + ultranet_rx_offset_eh, &c);

    // Preamble codes: X reg = 0x1F (Z code 0xF + '1' bits), Y reg = 0x6, OSR = 0x9.
    pio_sm_exec(pio, sm, pio_encode_set(pio_x, 0x1F));
    pio_sm_exec(pio, sm, pio_encode_set(pio_y, 0x6));
    pio_sm_put(pio, sm, UN_CODE_X);
    pio_sm_exec(pio, sm, pio_encode_pull(false, true));

    // Only now join the FIFOs (joining discards the TX FIFO we just used).
    sm_config_set_fifo_join(&c, PIO_FIFO_JOIN_RX);
    pio_sm_set_config(pio, sm, &c);
}

static void pacer_sm_init(uint offset) {
    pio_sm_config c = pacer_program_get_default_config(offset);
    // 498 cycles per tick; 196.5 MHz / 384 kHz = 511.7 -> divide by 1 + 7/256
    sm_config_set_clkdiv_int_frac8(&c, 1, 7);
    pio_sm_init(PIO_RX, SM_PACER, offset, &c);
}

static void out_sm_init(uint offset) {
    PIO pio = PIO_OUT;
    for (uint p = PIN_I2S_SD0; p <= PIN_MCLK; p++) {
        pio_gpio_init(pio, p);
        gpio_set_drive_strength(p, GPIO_DRIVE_STRENGTH_8MA);
        gpio_set_slew_rate(p, GPIO_SLEW_RATE_FAST);
    }
    pio_sm_set_consecutive_pindirs(pio, SM_OUT, PIN_I2S_SD0, PIN_MCLK - PIN_I2S_SD0 + 1, true);

    pio_sm_config c = i2s8_out_program_get_default_config(offset);
    sm_config_set_out_pins(&c, PIN_I2S_SD0, 9);            // SD0..SD7 + LRCLK
    sm_config_set_sideset_pins(&c, PIN_I2S_BCLK);          // BCLK, MCLK
    sm_config_set_out_shift(&c, true /*right*/, true /*autopull*/, 32);
    sm_config_set_fifo_join(&c, PIO_FIFO_JOIN_TX);
    // 130 SM cycles per 8-bit chunk * 3.883 = ~505 sys cycles < 511.7 per subframe
    sm_config_set_clkdiv_int_frac8(&c, 3, 226);
    pio_sm_init(pio, SM_OUT, offset, &c);
}

// ======================================================================
// DMA
// ======================================================================

static int rx_dma_start(uint idx, uint sm) {
    int ch = dma_claim_unused_channel(true);
    dma_channel_config c = dma_channel_get_default_config(ch);
    channel_config_set_transfer_data_size(&c, DMA_SIZE_32);
    channel_config_set_read_increment(&c, false);
    channel_config_set_write_increment(&c, true);
    channel_config_set_ring(&c, true /*write*/, RX_RING_LOG2);
    channel_config_set_dreq(&c, pio_get_dreq(PIO_RX, sm, false));
    dma_channel_configure(ch, &c, rx_ring[idx], &PIO_RX->rxf[sm], DMA_ENDLESS, true);
    return ch;
}

static int out_dma_start(void) {
    int ch = dma_claim_unused_channel(true);
    dma_channel_config c = dma_channel_get_default_config(ch);
    channel_config_set_transfer_data_size(&c, DMA_SIZE_32);
    channel_config_set_read_increment(&c, true);
    channel_config_set_write_increment(&c, false);
    channel_config_set_ring(&c, false /*read*/, OUT_RING_LOG2);
    channel_config_set_dreq(&c, pio_get_dreq(PIO_OUT, SM_OUT, true));
    dma_channel_configure(ch, &c, &PIO_OUT->txf[SM_OUT], out_ring, DMA_ENDLESS, true);
    return ch;
}

static inline uint32_t rx_wr_index(uint i) {
    return ((uint32_t)dma_hw->ch[dma_rx[i]].write_addr - (uint32_t)rx_ring[i]) / 4u;
}
static inline uint32_t out_rd_index(void) {
    return ((uint32_t)dma_hw->ch[dma_out].read_addr - (uint32_t)out_ring) / 4u;
}

// ======================================================================
// Core 1: data path
// ======================================================================

static uint32_t out_wr;     // word index, always a multiple of FRAME_WORDS

static inline uint32_t out_level(void) {
    return (out_wr - out_rd_index()) & (OUT_RING_WORDS - 1);
}

static void out_put_frame(const uint32_t ch16[16]) {
    uint32_t lvl = out_level();
    if (lvl > OUT_RING_WORDS / 2) {
        // reader overtook the writer (should only happen after a stall): re-centre
        out_wr = (out_rd_index() + OUT_TARGET + FRAME_WORDS) & ~(FRAME_WORDS - 1) & (OUT_RING_WORDS - 1);
        out_resyncs++;
        lvl = out_level();
    }
    if (lvl > OUT_HIGH) {               // too far ahead: drop this frame
        out_drops++;
        return;
    }
    un_pack_i2s8(ch16, &out_ring[out_wr]);
    out_wr = (out_wr + FRAME_WORDS) & (OUT_RING_WORDS - 1);
    if (lvl < OUT_LOW) {                // close to underflow: repeat the frame
        un_pack_i2s8(ch16, &out_ring[out_wr]);
        out_wr = (out_wr + FRAME_WORDS) & (OUT_RING_WORDS - 1);
        out_dups++;
    }
}

static void core1_main(void) {
    static const uint32_t silence[16];
    uint32_t rd[2] = { rx_wr_index(0), rx_wr_index(1) };
    uint32_t ch16[16];

    for (;;) {
        // Stream B first, so the frame paired with A is as fresh as possible.
        uint32_t w = rx_wr_index(1);
        while (rd[1] != w) {
            if (un_stream_push(&st[1], rx_ring[1][rd[1]]))
                last_frame_us[1] = time_us_32();
            rd[1] = (rd[1] + 1) & (RX_RING_WORDS - 1);
        }
        uint32_t now = time_us_32();
        bool b_alive = (now - last_frame_us[1]) < 5000;

        w = rx_wr_index(0);
        while (rd[0] != w) {
            if (un_stream_push(&st[0], rx_ring[0][rd[0]])) {
                memcpy(ch16, st[0].frame, sizeof st[0].frame);
                if (b_alive) memcpy(ch16 + 8, st[1].frame, sizeof st[1].frame);
                else         memset(ch16 + 8, 0, 8 * sizeof(uint32_t));
                out_put_frame(ch16);
                last_frame_us[0] = time_us_32();
            }
            rd[0] = (rd[0] + 1) & (RX_RING_WORDS - 1);
        }

        // No stream A: keep the I2S clocks running from the pacer, send silence
        // (or stream B alone, if present).
        now = time_us_32();
        if ((now - last_frame_us[0]) > 5000) {
            if (!pacer_on) {
                pio_sm_set_enabled(PIO_RX, SM_PACER, true);
                pacer_on = true;
            }
            while (out_level() < OUT_TARGET) {
                if (b_alive) {
                    memset(ch16, 0, 8 * sizeof(uint32_t));
                    memcpy(ch16 + 8, st[1].frame, sizeof st[1].frame);
                    out_put_frame(ch16);
                } else {
                    out_put_frame(silence);
                }
            }
        } else if (pacer_on) {
            pio_sm_set_enabled(PIO_RX, SM_PACER, false);
            pacer_on = false;
        }
    }
}

// ======================================================================
// Core 0: setup + monitoring
// ======================================================================

static float dbfs(uint32_t peak) {
    if (peak < 1024) return -120.0f;
    return 20.0f * log10f((float)peak / 2147483648.0f);
}

int main(void) {
    set_sys_clock_khz(SYS_KHZ, true);
    stdio_init_all();

    gpio_init(PICO_DEFAULT_LED_PIN);
    gpio_set_dir(PICO_DEFAULT_LED_PIN, GPIO_OUT);

    un_stream_init(&st[0]);
    un_stream_init(&st[1]);

    uint rx_off = pio_add_program(PIO_RX, &ultranet_rx_program);
    pacer_offset = pio_add_program(PIO_RX, &pacer_program);
    uint out_off = pio_add_program(PIO_OUT, &i2s8_out_program);

    rx_sm_init(SM_RX_A, rx_off, PIN_UNET_A);
    rx_sm_init(SM_RX_B, rx_off, PIN_UNET_B);
    pacer_sm_init(pacer_offset);
    out_sm_init(out_off);

    // Pre-fill the whole output ring with properly framed silence.
    static const uint32_t silence[16];
    for (uint32_t i = 0; i < OUT_RING_WORDS; i += FRAME_WORDS)
        un_pack_i2s8(silence, &out_ring[i]);
    out_wr = OUT_TARGET;

    dma_rx[0] = rx_dma_start(0, SM_RX_A);
    dma_rx[1] = rx_dma_start(1, SM_RX_B);
    dma_out = out_dma_start();

    last_frame_us[0] = last_frame_us[1] = time_us_32() - 1000000;
    pio_sm_set_enabled(PIO_RX, SM_PACER, true);
    pacer_on = true;
    pio_sm_set_enabled(PIO_OUT, SM_OUT, true);
    pio_set_sm_mask_enabled(PIO_RX, (1u << SM_RX_A) | (1u << SM_RX_B), true);

    multicore_launch_core1(core1_main);

    // ---- monitoring loop ----
    const uint pins[2] = { PIN_UNET_A, PIN_UNET_B };
    bool inverted[2] = { false, false };
    uint32_t last_frames[2] = { 0, 0 };
    uint32_t prev[2][8];
    memset(prev, 0, sizeof prev);
    absolute_time_t next_report = make_timeout_time_ms(2000);

    for (;;) {
        sleep_ms(250);
        uint32_t now = time_us_32();

        // Polarity auto-detect: no complete frames for 250 ms -> flip input.
        for (int i = 0; i < 2; i++) {
            uint32_t f = st[i].frames;
            bool alive = (now - last_frame_us[i]) < 250000;
            if (!alive && f == last_frames[i]) {
                inverted[i] = !inverted[i];
                gpio_set_inover(pins[i], inverted[i] ? GPIO_OVERRIDE_INVERT : GPIO_OVERRIDE_NORMAL);
            }
            last_frames[i] = f;
        }
        gpio_put(PICO_DEFAULT_LED_PIN, (now - last_frame_us[0]) < 100000);

        if (absolute_time_diff_us(get_absolute_time(), next_report) > 0)
            continue;
        next_report = make_timeout_time_ms(2000);

        printf("\n=== Ultranet bridge  (out level %lu words, dup %lu, drop %lu, resync %lu, %s)\n",
               (unsigned long)out_level(), (unsigned long)out_dups, (unsigned long)out_drops,
               (unsigned long)out_resyncs, pacer_on ? "FREE-RUN" : "LOCKED");
        for (int i = 0; i < 2; i++) {
            un_stream_t *s = &st[i];
            uint32_t cur[8] = { s->words, s->frames, s->code_err, s->parity_err,
                                s->order_err, s->incomplete, s->z_ch1, s->z_other };
            uint32_t d[8];
            for (int k = 0; k < 8; k++) { d[k] = cur[k] - prev[i][k]; prev[i][k] = cur[k]; }
            printf("stream %c (ch %2d-%2d) %s pol=%s  frames/s=%lu  err: code=%lu par=%lu order=%lu inc=%lu  Z@ch1=%lu Z@other=%lu\n",
                   'A' + i, 1 + 8 * i, 8 + 8 * i,
                   (now - last_frame_us[i]) < 100000 ? "OK  " : "--  ",
                   inverted[i] ? "inv" : "norm",
                   (unsigned long)(d[1] / 2), (unsigned long)d[2], (unsigned long)d[3],
                   (unsigned long)d[4], (unsigned long)d[5], (unsigned long)d[6], (unsigned long)d[7]);
            uint32_t pk[8];
            un_stream_take_peaks(s, pk);
            printf("   peaks dBFS:");
            for (int k = 0; k < 8; k++) printf(" %6.1f", (double)dbfs(pk[k]));
            printf("\n");
        }
    }
}
