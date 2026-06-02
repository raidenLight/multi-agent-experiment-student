"""V4 策略入口：碰撞预警 + 低收益车辆重规划。"""

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
    print("=== V4 碰撞预警重规划策略 ===")
    print("连接到:", SERVER_URL)
    sdk.run(my_strategy)
