"""Compatibility import for the isolated DUAL40 LIVE gateway.

All signing and CLOB order submission remains under the ``p3_live_*`` namespace so
the P3 research core stays execution-client-free.
"""
from p3_live_dual40_gateway import Dual40Gateway

__all__ = ["Dual40Gateway"]
