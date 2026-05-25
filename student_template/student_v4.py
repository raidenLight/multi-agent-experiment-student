"""V4 策略入口：拥堵感知路径——带节点/边惩罚的 Dijkstra 绕行。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from student_template.strategy import V4Strategy
from student_template.strategy.sdk_ext import StrategySDK


SERVER_URL = "ws://localhost:8765"
sdk = StrategySDK(SERVER_URL)
strategy = V4Strategy(sdk)


def my_strategy(state):
    return strategy(state)


if __name__ == "__main__":
    print("=== V4 拥堵感知路径策略 ===")
    print("连接到:", SERVER_URL)
    sdk.run(my_strategy)
