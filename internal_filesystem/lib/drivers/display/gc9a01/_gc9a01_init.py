# Copyright (c) 2024 - 2025 Kevin G. Schlosser

import time
from micropython import const  # NOQA

import lvgl as lv  # NOQA


_SWRESET = const(0x01)
_SLPOUT  = const(0x11)
_INVON   = const(0x21)
_DISPON  = const(0x29)
_CASET   = const(0x2A)
_RASET   = const(0x2B)
_MADCTL  = const(0x36)
_COLMOD  = const(0x3A)

# GC9A01 inter-register enable commands
_INREGEN1 = const(0xFE)
_INREGEN2 = const(0xEF)


def init(self):
    param_buf = bytearray(16)
    param_mv = memoryview(param_buf)

    self.set_params(_SWRESET)
    time.sleep_ms(150)  # NOQA

    self.set_params(_SLPOUT)
    time.sleep_ms(120)  # NOQA

    # Enable access to manufacturer commands
    self.set_params(_INREGEN1)
    self.set_params(_INREGEN2)

    # Power control registers
    param_buf[0] = 0x20
    self.set_params(0xEB, param_mv[:1])

    param_buf[0] = 0xC0
    self.set_params(0xA4, param_mv[:1])

    param_buf[0] = 0x40
    self.set_params(0xC5, param_mv[:1])

    # Display function control
    param_buf[0] = 0x00
    param_buf[1] = 0x20
    self.set_params(0xB6, param_mv[:2])

    # MADCTL - memory access control (rotation/mirroring)
    param_buf[0] = self._madctl(
        self._color_byte_order,
        self._ORIENTATION_TABLE  # NOQA
    )
    self.set_params(_MADCTL, param_mv[:1])

    # COLMOD - pixel format
    color_size = lv.color_format_get_size(self._color_space)
    if color_size == 2:
        param_buf[0] = 0x55  # 16-bit RGB565
    elif color_size == 3:
        param_buf[0] = 0x77  # 24-bit RGB888
    else:
        raise RuntimeError(
            f'{self.__class__.__name__} IC only supports '
            'lv.COLOR_FORMAT.RGB565 or lv.COLOR_FORMAT.RGB888'
        )
    self.set_params(_COLMOD, param_mv[:1])

    # VREG control
    param_buf[0] = 0x08
    param_buf[1] = 0x08
    param_buf[2] = 0x08
    param_buf[3] = 0x08
    self.set_params(0x90, param_mv[:4])

    param_buf[0] = 0x06
    self.set_params(0xBD, param_mv[:1])

    param_buf[0] = 0x00
    self.set_params(0xBC, param_mv[:1])

    param_buf[0] = 0x60
    param_buf[1] = 0x01
    param_buf[2] = 0x04
    self.set_params(0xFF, param_mv[:3])

    param_buf[0] = 0x13
    self.set_params(0xC3, param_mv[:1])

    param_buf[0] = 0x13
    self.set_params(0xC4, param_mv[:1])

    param_buf[0] = 0x22
    self.set_params(0xC9, param_mv[:1])

    param_buf[0] = 0x11
    self.set_params(0xBE, param_mv[:1])

    param_buf[0] = 0x10
    param_buf[1] = 0x0E
    self.set_params(0xE1, param_mv[:2])

    param_buf[0] = 0x21
    param_buf[1] = 0x0C
    param_buf[2] = 0x02
    self.set_params(0xDF, param_mv[:3])

    # Positive gamma correction
    param_buf[0] = 0x45
    param_buf[1] = 0x09
    param_buf[2] = 0x08
    param_buf[3] = 0x08
    param_buf[4] = 0x26
    param_buf[5] = 0x2A
    self.set_params(0xF0, param_mv[:6])

    # Negative gamma correction
    param_buf[0] = 0x43
    param_buf[1] = 0x70
    param_buf[2] = 0x72
    param_buf[3] = 0x36
    param_buf[4] = 0x37
    param_buf[5] = 0x6F
    self.set_params(0xF1, param_mv[:6])

    # Positive gamma correction (set 2)
    param_buf[0] = 0x45
    param_buf[1] = 0x09
    param_buf[2] = 0x08
    param_buf[3] = 0x08
    param_buf[4] = 0x26
    param_buf[5] = 0x2A
    self.set_params(0xF2, param_mv[:6])

    # Negative gamma correction (set 2)
    param_buf[0] = 0x43
    param_buf[1] = 0x70
    param_buf[2] = 0x72
    param_buf[3] = 0x36
    param_buf[4] = 0x37
    param_buf[5] = 0x6F
    self.set_params(0xF3, param_mv[:6])

    # Frame rate control
    param_buf[0] = 0x34
    self.set_params(0xE8, param_mv[:1])

    # Column address set
    param_buf[0] = 0x00
    param_buf[1] = 0x00
    param_buf[2] = (self.display_width >> 8) & 0xFF
    param_buf[3] = self.display_width & 0xFF
    self.set_params(_CASET, param_mv[:4])

    # Row address set
    param_buf[0] = 0x00
    param_buf[1] = 0x00
    param_buf[2] = (self.display_height >> 8) & 0xFF
    param_buf[3] = self.display_height & 0xFF
    self.set_params(_RASET, param_mv[:4])

    # Color inversion ON (required for correct colors on GC9A01)
    self.set_params(_INVON)

    self.set_params(_DISPON)
    time.sleep_ms(120)  # NOQA

    self.set_params(_SLPOUT)
    time.sleep_ms(120)  # NOQA
