"""Read the current process's own creation time, including in Linux containers."""

import os
import sys
from pathlib import Path

from . import process_os as psutil


def current_creation_time():
    if sys.platform == "linux":
        # /proc/self addresses this process even when /proc lists host PIDs and
        # os.getpid() belongs to a nested namespace. Looking up that small PID
        # could otherwise return an unrelated host process or fail at startup.
        stat = Path("/proc/self/stat").read_text(encoding="utf-8", errors="surrogateescape")
        after_name = stat[stat.rfind(")") + 2 :].split()
        ticks = int(after_name[19])
        boot = psutil.boot_time()
        return boot + ticks / os.sysconf("SC_CLK_TCK")
    return psutil.Process(os.getpid()).create_time()
