"""V9 strategy entry: adapted V5-VN snapshot 3768 on seed100."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from student_template.strategy import V9Strategy
from student_template.strategy.sdk_ext import StrategySDK


SERVER_URL = "ws://localhost:8765"
sdk = StrategySDK(SERVER_URL)
strategy = V9Strategy(sdk)


def my_strategy(state):
    return strategy(state)


if __name__ == "__main__":
    print("=== V9 V5-VN snapshot 3768 ===")
    print("Connecting to:", SERVER_URL)
    sdk.run(my_strategy)
