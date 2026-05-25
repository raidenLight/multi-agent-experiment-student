"""V5 策略入口：极近距离防撞减速——最后一道安全防线。"""

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
    print("=== V5 极近距离防撞策略 ===")
    print("连接到:", SERVER_URL)
    sdk.run(my_strategy)
