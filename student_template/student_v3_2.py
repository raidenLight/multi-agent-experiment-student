"""V3_2 策略入口：匈牙利全局最优分配 + 拥堵感知 + 链完成增强。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from student_template.strategy import V3_2Strategy
from student_template.strategy.sdk_ext import StrategySDK


SERVER_URL = "ws://localhost:8765"
sdk = StrategySDK(SERVER_URL)
strategy = V3_2Strategy(sdk)


def my_strategy(state):
    return strategy(state)


if __name__ == "__main__":
    print("=== V3_2 匈牙利全局最优 + 拥堵感知 + 链完成增强 ===")
    print("连接到:", SERVER_URL)
    print("策略：匈牙利全局最优匹配 + 拥堵惩罚 + 链完成最后一块奖励")
    print("等待游戏开始...")
    sdk.run(my_strategy)
