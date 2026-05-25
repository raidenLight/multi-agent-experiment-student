"""V5 策略入口：时空联合路径规划——时间线冲突检测 + 分级错峰。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from student_template.strategy import V5Strategy
from student_template.strategy.sdk_ext import StrategySDK


SERVER_URL = "ws://localhost:8765"
sdk = StrategySDK(SERVER_URL)
strategy = V5Strategy(sdk)


def my_strategy(state):
    return strategy(state)


if __name__ == "__main__":
    print("=== V5 时空联合路径规划策略 ===")
    print("连接到:", SERVER_URL)
    sdk.run(my_strategy)
