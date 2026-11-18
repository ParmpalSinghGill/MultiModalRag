"""
Root bridge for DownloadData.py.
Delegates execution to data_preparation/DownloadData.py.
"""

import sys
import runpy
from pathlib import Path

target = Path(__file__).resolve().parent / "data_preparation" / "DownloadData.py"
runpy.run_path(str(target), run_name="__main__")
