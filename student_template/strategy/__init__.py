"""学生策略可复用模块。"""

from .strategy import V1Strategy, V2Strategy, V3Strategy, V3_1Strategy, V3_2Strategy, V4Strategy, V5Strategy, VNStrategy
from .v5_low_score import (
    V5VN3170Strategy as V5Strategy,
    V5VN3280Strategy as V6Strategy,
    V5VN3457Strategy as V7Strategy,
    V5VN3590Strategy as V8Strategy,
    V5VN3768Strategy as V9Strategy,
)
__all__ = [
    "V0Strategy",
    "V1Strategy",
    "V2Strategy",
    "V3Strategy",
    "V3_1Strategy",
    "V3_2Strategy",
    "V4Strategy",
    "V5Strategy",
    "V6Strategy",
    "V7Strategy",
    "V8Strategy",
    "V9Strategy",
    "VNStrategy",
]
