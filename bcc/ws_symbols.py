"""Shared USDT topic identity for public stream subscriptions.

Single-character asset codes are valid (AUSDT, 4USDT). This checks topic
syntax only; instruments, venue policy and executable data remain separately
validated before PAPER decisions.
"""
import re

USDT_SYMBOL = re.compile(r"[A-Z0-9]{1,24}USDT\Z")
