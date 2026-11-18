"""
Root bridge for upload_s3.py.
Delegates execution to data_preparation/upload_s3.py.
"""

import sys
import runpy
from pathlib import Path

target = Path(__file__).resolve().parent / "data_preparation" / "upload_s3.py"
runpy.run_path(str(target), run_name="__main__")
