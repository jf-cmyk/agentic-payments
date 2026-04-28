"""Compatibility wrapper for the safe SOL sweep utility.

Private key material must be supplied with SWEEP_PRIVATE_KEY_BASE64. This file
intentionally contains no embedded wallet keys or RPC API keys.
"""

from __future__ import annotations

import asyncio

from sweep_dust import main


if __name__ == "__main__":
    asyncio.run(main())
