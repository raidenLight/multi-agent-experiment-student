"""VN 最终策略入口：超时任务释放——久未完成的任务强制释放重新调度。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from student_template.strategy import VNStrategy
from student_template.strategy.sdk_ext import StrategySDK


SERVER_URL = "ws://localhost:8765"
sdk = StrategySDK(SERVER_URL)
strategy = VNStrategy(sdk)


def my_strategy(state):
    return strategy(state)


if __name__ == "__main__":
    print("=== VN 超时任务释放策略 ===")
    print("连接到:", SERVER_URL)
    sdk.run(my_strategy)
