"""V1 学生策略入口。

V1 保持 V0 的贪心任务顺序，但加入集中式目标占用和统一的 `target_zone`
动作，减少重复取货、重复投递和误触发交互。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from student_template.strategy import V1Strategy
from student_template.strategy.sdk_ext import StrategySDK


SERVER_URL = "ws://localhost:8765"
sdk = StrategySDK(SERVER_URL)
strategy = V1Strategy(sdk)


def my_strategy(state):
    """服务端回调入口，保留这个名字以兼容 SDK。"""
    return strategy(state)


if __name__ == "__main__":
    print("=== V1 协同供应链策略 ===")
    print("连接到:", SERVER_URL)
    print("策略：V0 贪心 + target_zone + claimed/in_transit 目标占用")
    print("等待游戏开始...")
    sdk.run(my_strategy)
