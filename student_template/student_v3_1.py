"""V3_1 策略入口：拥堵感知分配 + 增强链完成。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from student_template.strategy import V3_1Strategy
from student_template.strategy.sdk_ext import StrategySDK


SERVER_URL = "ws://localhost:8765"
sdk = StrategySDK(SERVER_URL)
strategy = V3_1Strategy(sdk)


def my_strategy(state):
    return strategy(state)


if __name__ == "__main__":
    print("=== V3_1 拥堵感知 + 链完成增强策略 ===")
    print("连接到:", SERVER_URL)
    print("策略：拥堵感知的全局贪心分配 + 链完成最后一块奖励")
    print("等待游戏开始...")
    sdk.run(my_strategy)
