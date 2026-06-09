if __debug__: logger.debug("spotpear_esp32_s3_1_28_round.py initialization")
# Hardware initialization for Spotpear ESP32-S3-N16R8 1.28" Round LCD Box
# Product page: https://spotpear.com/wiki/ESP32-S3-N16R8-AI-DeepSeek-XiaoZhi-XiaGe-Qwen-DouBao-1.28-inch-Round-LCD-BOX-TouchScreen.html
#
# Hardware specifications (from Xiaozhi ESP32 firmware, board: sp-esp32-s3-1.28-box):
#   MCU:     ESP32-S3-N16R8 (16 MB Flash, 8 MB Octal PSRAM, 240 MHz)
#   Display: GC9A01A, 240×240 round LCD, SPI
#     SCLK=4  MOSI=2  CS=5  DC=47  RST=38  BL=42 (active-low)
#   Touch:   CST816D capacitive, I2C addr 0x15
#     SDA=11  SCL=7  RST=6  INT=12
#   Audio codec: ES8311, I2C addr 0x18
#     I2C SDA=15  SCL=14
#     I2S  MCLK=16  BCLK=9  WS=45  DOUT=8  DIN=10  PA=46
#   Battery: ADC on GPIO 1, charging-detect on GPIO 41
#   LED:     GPIO 48
#   Button:  GPIO 0 (BOOT, active-low)

import logging

logger = logging.getLogger(__name__)

import time

import drivers.display.gc9a01 as gc9a01
import drivers.indev.cst816d as cst816d
import i2c
import lcd_bus
import lvgl as lv
import machine
import mpos.ui
import pointer_framework
from machine import Pin
from micropython import const
from mpos import BatteryManager, InputManager

# ─── Display SPI pins ───────────────────────────────────────────────────────
SPI_BUS  = const(1)
SPI_FREQ = const(40_000_000)
LCD_SCLK = const(4)
LCD_MOSI = const(2)
LCD_CS   = const(5)
LCD_DC   = const(47)
LCD_RST  = const(38)
LCD_BL   = const(42)   # active-low: LOW turns backlight ON

# ─── Touch I2C pins ─────────────────────────────────────────────────────────
TOUCH_SDA = const(11)
TOUCH_SCL = const(7)
TOUCH_RST = const(6)
TOUCH_INT = const(12)
TOUCH_I2C_FREQ = const(400_000)

# ─── Audio pins ─────────────────────────────────────────────────────────────
CODEC_SDA = const(15)
CODEC_SCL = const(14)
I2S_MCLK  = const(16)
I2S_BCLK  = const(9)
I2S_WS    = const(45)
I2S_DOUT  = const(8)   # ESP32 → codec DAC → speaker
I2S_DIN   = const(10)  # codec ADC → ESP32 (microphone)
AMP_PA    = const(46)  # power amplifier enable (active-high)

# ─── Misc ────────────────────────────────────────────────────────────────────
BATTERY_ADC_PIN     = const(1)
BATTERY_CHARGE_PIN  = const(41)
BOOT_BUTTON_PIN     = const(0)
LED_PIN             = const(48)

# ─── Display resolution ──────────────────────────────────────────────────────
LCD_WIDTH  = const(240)
LCD_HEIGHT = const(240)

# ==============================
# Step 1: Display (GC9A01A, SPI)
# ==============================
if __debug__: logger.debug("spotpear_esp32_s3_1_28_round: init SPI display (GC9A01A 240×240)")

try:
    spi_bus = machine.SPI.Bus(host=SPI_BUS, mosi=LCD_MOSI, sck=LCD_SCLK)
except Exception as e:
    logger.error("SPI bus init failed: %s" % e)
    time.sleep(3)
    machine.reset()

display_bus = lcd_bus.SPIBus(
    spi_bus=spi_bus,
    freq=SPI_FREQ,
    dc=LCD_DC,
    cs=LCD_CS,
)

# 240×240 @ RGB565 = 115 200 bytes total; use ~1/8 as DMA buffer
_BUFFER_SIZE = const(240 * 30 * 2)  # 14 400 bytes
fb1 = display_bus.allocate_framebuffer(_BUFFER_SIZE, lcd_bus.MEMORY_INTERNAL | lcd_bus.MEMORY_DMA)
fb2 = display_bus.allocate_framebuffer(_BUFFER_SIZE, lcd_bus.MEMORY_INTERNAL | lcd_bus.MEMORY_DMA)

mpos.ui.main_display = gc9a01.GC9A01(
    data_bus=display_bus,
    frame_buffer1=fb1,
    frame_buffer2=fb2,
    display_width=LCD_WIDTH,
    display_height=LCD_HEIGHT,
    reset_pin=LCD_RST,
    backlight_pin=LCD_BL,
    backlight_on_state=gc9a01.STATE_LOW,   # GPIO 42 is active-low for backlight
    color_space=lv.COLOR_FORMAT.RGB565,
    color_byte_order=gc9a01.BYTE_ORDER_BGR,
    rgb565_byte_swap=True,
)  # triggers lv.init()

mpos.ui.main_display.init()
mpos.ui.main_display.set_power(True)
mpos.ui.main_display.set_backlight(100)

# ==============================
# Step 2: Touch (CST816D, I2C)
# ==============================
if __debug__: logger.debug("spotpear_esp32_s3_1_28_round: init touch (CST816D)")

# Hardware-reset the touch controller before I2C init
touch_rst_pin = Pin(TOUCH_RST, Pin.OUT)
touch_rst_pin.value(0)
time.sleep_ms(5)
touch_rst_pin.value(1)
time.sleep_ms(50)

machine_i2c = machine.I2C(0, sda=Pin(TOUCH_SDA), scl=Pin(TOUCH_SCL), freq=TOUCH_I2C_FREQ)
i2c_bus = i2c.I2C.Bus(host=0, sda=TOUCH_SDA, scl=TOUCH_SCL, freq=TOUCH_I2C_FREQ, use_locks=False)
i2c_bus._bus = machine_i2c

touch_dev = i2c.I2C.Device(bus=i2c_bus, dev_id=cst816d.I2C_ADDR, reg_bits=cst816d.BITS)
try:
    indev = cst816d.CST816D(
        touch_dev,
        # reset already done above; pass None to avoid a second reset cycle
        reset_pin=None,
        startup_rotation=pointer_framework.lv.DISPLAY_ROTATION._0,
    )
    InputManager.register_indev(indev)
except Exception as e:
    logger.error("CST816D touch init failed: %s" % e)

# ==============================
# Step 3: Boot button (GPIO 0)
# ==============================
if __debug__: logger.debug("spotpear_esp32_s3_1_28_round: init boot button")

btn_boot = Pin(BOOT_BUTTON_PIN, Pin.IN, Pin.PULL_UP)

_last_key   = None
_last_state = lv.INDEV_STATE.RELEASED
_key_start  = 0
_last_rep   = 0

REPEAT_INITIAL_MS = const(300)
REPEAT_RATE_MS    = const(100)

def _keypad_read_cb(indev, data):
    global _last_key, _last_state, _key_start, _last_rep

    now = time.ticks_ms()
    key = lv.KEY.ESC if btn_boot.value() == 0 else None

    if key is None:
        data.key   = _last_key if _last_key else lv.KEY.ESC
        data.state = lv.INDEV_STATE.RELEASED
        _last_key  = None
        _last_state = lv.INDEV_STATE.RELEASED
        _key_start  = 0
        _last_rep   = 0
    elif _last_key is None or key != _last_key:
        data.key    = key
        data.state  = lv.INDEV_STATE.PRESSED
        _last_key   = key
        _last_state = lv.INDEV_STATE.PRESSED
        _key_start  = now
        _last_rep   = now
    else:
        elapsed  = time.ticks_diff(now, _key_start)
        since_rp = time.ticks_diff(now, _last_rep)
        if elapsed >= REPEAT_INITIAL_MS and since_rp >= REPEAT_RATE_MS:
            data.key   = key
            data.state = (lv.INDEV_STATE.PRESSED
                          if _last_state == lv.INDEV_STATE.RELEASED
                          else lv.INDEV_STATE.RELEASED)
            _last_state = data.state
            _last_rep   = now
        else:
            data.state = lv.INDEV_STATE.RELEASED
            _last_state = lv.INDEV_STATE.RELEASED

    if _last_state == lv.INDEV_STATE.PRESSED and key == lv.KEY.ESC:
        mpos.ui.back_screen()

group    = lv.group_get_default()
btn_indev = lv.indev_create()
btn_indev.set_type(lv.INDEV_TYPE.KEYPAD)
btn_indev.set_read_cb(_keypad_read_cb)
btn_indev.set_group(group)
btn_indev.set_display(lv.display_get_default())
btn_indev.enable(True)
InputManager.register_indev(btn_indev)

# ==============================
# Step 4: Battery ADC (GPIO 1)
# ==============================
if __debug__: logger.debug("spotpear_esp32_s3_1_28_round: init battery ADC")

def _adc_to_voltage(adc_value):
    # Typical 1:2 voltage divider on ESP32-S3 battery boards.
    # V_bat = V_adc × 2 ; V_adc = adc_value / 4095 × 3.3 V
    # Calibrate by measuring actual battery voltage vs. ADC reading.
    return adc_value * (3.3 / 4095) * 2

BatteryManager.init_adc(BATTERY_ADC_PIN, _adc_to_voltage)

# ==============================
# Step 5: Audio (ES8311 + PA)
# ==============================
if __debug__: logger.debug("spotpear_esp32_s3_1_28_round: init audio (ES8311)")

_codec_i2c = machine.I2C(1, sda=Pin(CODEC_SDA), scl=Pin(CODEC_SCL))
_es8311 = None
try:
    import drivers.codec.es8311 as es8311_drv
    _es8311 = es8311_drv.ES8311(_codec_i2c)
except Exception as e:
    logger.error("ES8311 init failed: %s" % e)

_amp_pa = Pin(AMP_PA, Pin.OUT, value=0)  # LOW = amplifier disabled at boot

def _audio_on_open():
    _amp_pa.value(1)   # enable PA
    if _es8311:
        time.sleep_ms(10)
        _es8311.dac_mute(False)

def _audio_on_close():
    if _es8311:
        _es8311.dac_mute(True)
        time.sleep_ms(20)
    _amp_pa.value(0)   # disable PA

from mpos import AudioManager

AudioManager.add(
    AudioManager.Output(
        name="Speaker",
        kind="i2s",
        channels=1,
        i2s_pins={
            'mck': I2S_MCLK,
            'sck': I2S_BCLK,
            'ws':  I2S_WS,
            'sd':  I2S_DOUT,
        },
        on_open=_audio_on_open,
        on_close=_audio_on_close,
    )
)

AudioManager.add(
    AudioManager.Input(
        name="Microphone",
        kind="i2s",
        channels=1,
        i2s_pins={
            'mck':   I2S_MCLK,
            'sck':   I2S_BCLK,
            'ws':    I2S_WS,
            'sd_in': I2S_DIN,
        },
        preferred_sample_rate=16000,
    )
)

if __debug__: logger.debug("spotpear_esp32_s3_1_28_round.py finished")
