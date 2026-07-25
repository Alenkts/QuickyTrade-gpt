#!/usr/bin/env python3
"""Retired compatibility resource; broker execution must use ``core/``."""

import json


if __name__ == "__main__":
    print(json.dumps({
        "status": "Legacy native IBKR bridge retired; use the durable web/core paper pipeline"
    }), flush=True)
    raise SystemExit(78)
