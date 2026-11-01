// No codec to configure: the I2S source is the RP2350 Ultranet decoder,
// which runs on its own and is always the clock master.
#include "xua.h"

void AudioHwInit()
{
}

void AudioHwConfig(unsigned samFreq, unsigned mClk, unsigned dsdMode,
                   unsigned sampRes_DAC, unsigned sampRes_ADC)
{
}
