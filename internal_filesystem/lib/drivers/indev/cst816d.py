# Copyright (c) 2024 - 2025 Kevin G. Schlosser

from micropython import const  # NOQA
import pointer_framework
import time
import machine  # NOQA

# Register map matches CST816S
I2C_ADDR = const(0x15)
BITS = const(8)

_GestureID  = const(0x01)
_FingerNum  = const(0x02)
_XposH      = const(0x03)
_XposL      = const(0x04)
_YposH      = const(0x05)
_YposL      = const(0x06)
_ChipID     = const(0xA7)
_FwVersion  = const(0xA9)
_IrqCtl     = const(0xFA)
_EnTouch    = const(0x40)
_EnChange   = const(0x20)
_DisAutoSleep = const(0xFE)

# Known chip IDs for CST816S / CST816D family
_ACCEPTED_IDS = (0xB5, 0xB6, 0xB7)


class CST816D(pointer_framework.PointerDriver):

    def _read_reg(self, reg):
        self._tx_buf[0] = reg
        self._rx_buf[0] = 0x00
        self._device.write_readinto(self._tx_mv[:1], self._rx_mv[:1])

    def _write_reg(self, reg, value):
        self._tx_buf[0] = reg
        self._tx_buf[1] = value
        self._device.write(self._tx_mv[:2])

    def __init__(
        self,
        device,
        reset_pin=None,
        touch_cal=None,
        startup_rotation=pointer_framework.lv.DISPLAY_ROTATION._0,  # NOQA
        debug=False
    ):
        self._tx_buf = bytearray(2)
        self._tx_mv = memoryview(self._tx_buf)
        self._rx_buf = bytearray(1)
        self._rx_mv = memoryview(self._rx_buf)
        self._device = device

        if not isinstance(reset_pin, int):
            self._reset_pin = reset_pin
        else:
            self._reset_pin = machine.Pin(reset_pin, machine.Pin.OUT)

        if self._reset_pin:
            self._reset_pin.value(1)

        self.hw_reset()
        # Disable auto-sleep so the controller stays responsive
        self._write_reg(_DisAutoSleep, 0xFE)

        self._read_reg(_ChipID)
        chip_id = self._rx_buf[0]
        print('CST816D Chip ID:', hex(chip_id))

        self._read_reg(_FwVersion)
        print('CST816D FW Version:', hex(self._rx_buf[0]))

        if chip_id not in _ACCEPTED_IDS:
            raise RuntimeError(f'CST816D: unexpected chip id {hex(chip_id)}')

        self._write_reg(_IrqCtl, _EnTouch | _EnChange)

        super().__init__(
            touch_cal=touch_cal, startup_rotation=startup_rotation, debug=debug
        )

    def hw_reset(self):
        if self._reset_pin is None:
            return
        self._reset_pin(0)
        time.sleep_ms(5)   # NOQA
        self._reset_pin(1)
        time.sleep_ms(50)  # NOQA

    def _get_coords(self):
        self._read_reg(_FingerNum)
        if self._rx_buf[0] == 0:
            return None

        self._read_reg(_XposH)
        x = (self._rx_buf[0] & 0x0F) << 8
        self._read_reg(_XposL)
        x |= self._rx_buf[0]

        self._read_reg(_YposH)
        y = (self._rx_buf[0] & 0x0F) << 8
        self._read_reg(_YposL)
        y |= self._rx_buf[0]

        return self.PRESSED, x, y
