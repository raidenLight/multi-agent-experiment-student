"""V2 策略入口：收益 + 紧急度综合排序调度。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from student_template.strategy import V2Strategy
from student_template.strategy.sdk_ext import StrategySDK


SERVER_URL = "ws://localhost:8765"
sdk = StrategySDK(SERVER_URL)
strategy = V2Strategy(sdk)


def my_strategy(state):
    return strategy(state)


if __name__ == "__main__":
    print("=== V2 协同供应链策略占位 ===")
    print("连接到:", SERVER_URL)
    sdk.run(my_strategy)
