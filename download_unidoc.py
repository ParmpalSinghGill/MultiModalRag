"""
Root bridge for download_unidoc.py CLI.
Delegates execution to data_preparation/download_unidoc.py.
"""

import sys
import runpy
from pathlib import Path

target = Path(__file__).resolve().parent / "data_preparation" / "download_unidoc.py"
runpy.run_path(str(target), run_name="__main__")
