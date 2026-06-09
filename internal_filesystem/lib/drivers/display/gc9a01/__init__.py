import sys
from . import gc9a01
from . import _gc9a01_init

# Register _gc9a01_init in sys.modules so display_driver_framework can find it
# via __import__('_gc9a01_init')
sys.modules['_gc9a01_init'] = _gc9a01_init

__all__ = [
    'GC9A01',
    'STATE_HIGH',
    'STATE_LOW',
    'STATE_PWM',
    'BYTE_ORDER_RGB',
    'BYTE_ORDER_BGR',
]

GC9A01 = gc9a01.GC9A01
STATE_HIGH = gc9a01.STATE_HIGH
STATE_LOW = gc9a01.STATE_LOW
STATE_PWM = gc9a01.STATE_PWM
BYTE_ORDER_RGB = gc9a01.BYTE_ORDER_RGB
BYTE_ORDER_BGR = gc9a01.BYTE_ORDER_BGR
