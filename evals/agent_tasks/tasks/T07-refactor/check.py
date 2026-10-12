import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, HERE)
os.environ["CHECKLIB_DIR"] = HERE
from checklib import standard_check  # noqa: E402

standard_check()
