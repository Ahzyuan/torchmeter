from typing import Union
from enum import Enum, IntEnum, IntFlag, unique

@unique
class CountUnit(IntEnum):
    T:int = 1e12
    G:int = 1e9
    M:int = 1e6
    K:int = 1e3

@unique
class BinaryUnit(IntFlag):
    TiB:int = 2**40
    GiB:int = 2**30
    MiB:int = 2**20
    KiB:int = 2**10
    B:int   = 2**0

@unique
class TimeUnit(Enum):
    h:int  = 60**2
    min:int = 60**1
    s:int  = 60**0
    ms:float = 1e-3
    us:float = 1e-6
    ns:float = 1e-9

@unique
class SpeedUnit(IntEnum):
    TSamPS:int = 1e12
    GSamPS:int = 1e9
    MSamPS:int = 1e6
    KSamPS:int = 1e3
    SamPS:int  = 1e0

UNIT_TYPE = Union[CountUnit, BinaryUnit, TimeUnit, SpeedUnit]

def auto_unit(val:Union[int, float], unit_system=CountUnit) -> str:
    for unit in list(unit_system):
        if val >= unit.value:
            if val % unit.value:
                return f"{val / unit.value:.2f} {unit.name}"
            else:
                return f"{val // unit.value} {unit.name}"
    if isinstance(val, int):
        return str(val)
    else:
        return f"{val:.2f}"
