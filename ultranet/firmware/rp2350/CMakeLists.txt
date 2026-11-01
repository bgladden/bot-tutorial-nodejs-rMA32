cmake_minimum_required(VERSION 3.13)

# Raspberry Pi Pico 2 (RP2350, Arm cores).  Override with -DPICO_BOARD=...
set(PICO_BOARD pico2 CACHE STRING "Board type")

# Needs PICO_SDK_PATH (pico-sdk >= 2.1.0) in the environment or on the command line.
include($ENV{PICO_SDK_PATH}/external/pico_sdk_import.cmake)

project(ultranet_bridge C CXX ASM)
set(CMAKE_C_STANDARD 11)
pico_sdk_init()

add_executable(ultranet_bridge
    src/main.c
    src/ultranet_core.c
)

pico_generate_pio_header(ultranet_bridge ${CMAKE_CURRENT_LIST_DIR}/src/ultranet.pio)

target_compile_options(ultranet_bridge PRIVATE -Wall -Wextra -O2)

target_link_libraries(ultranet_bridge
    pico_stdlib
    pico_multicore
    hardware_pio
    hardware_dma
    hardware_clocks
)

# Status console on the Pico's own USB port (CDC); UART stays free.
pico_enable_stdio_usb(ultranet_bridge 1)
pico_enable_stdio_uart(ultranet_bridge 0)

pico_add_extra_outputs(ultranet_bridge)   # .uf2 for drag-and-drop flashing
