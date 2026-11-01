# Ultranet 16-channel USB bridge: XMOS xcore.ai (XU316) as USB Audio Class 2
# device, I2S *slave* to the RP2350 Ultranet decoder.
#
# This directory is an overlay for https://github.com/xmos/sw_usb_audio :
# copy it next to app_usb_aud_template inside a sw_usb_audio checkout and build
# with the XMOS XTC tools (see ../README.md).  NOT YET BUILT OR HW-TESTED.

cmake_minimum_required(VERSION 3.21)
include($ENV{XMOS_CMAKE_PATH}/xcommon.cmake)

set(APP_INCLUDES src src/core src/extensions)
set(XMOS_SANDBOX_DIR ${CMAKE_CURRENT_LIST_DIR}/../..)
include(${CMAKE_CURRENT_LIST_DIR}/../deps.cmake)

project(app_ultranet_usb)

set(APP_HW_TARGET ultranet_bridge.xn)

set(SW_USB_AUDIO_FLAGS ${EXTRA_BUILD_FLAGS} -O3 -report -lquadflash -g -Wall)

# Audio Class 2, async, I2S slave, 16 in / 0 out, 48 kHz only
set(APP_COMPILER_FLAGS_2ASi16o0 ${SW_USB_AUDIO_FLAGS}
    -DCODEC_MASTER=1            # RP2350 drives BCLK / LRCLK / MCLK
    -DNUM_USB_CHAN_IN=16
    -DI2S_CHANS_ADC=16          # 8 I2S data wires, 2 channels each
    -DNUM_USB_CHAN_OUT=0
    -DI2S_CHANS_DAC=0
    -DMIN_FREQ=48000
    -DMAX_FREQ=48000
    -DMIXER=0
    -DXUA_I2S_N_BITS=32)

XMOS_REGISTER_APP()
