
"""Replay runner skeleton (Step 2).

Step 2 scope:
- Define the orchestration boundaries and sequencing for a deterministic replay.
- Provide a clear interface and documentation so Step 3 can implement the loop
  without changing file/module structure.

Sequencing (planned for Step 3)
-------------------------------
For each timestamp t in the unified timeline:

1) For each symbol:
   a) Update market state (last trade/quote/mid) from bar(t)
   b) Process fills for active orders (respecting activation latency)
   c) Emit order update events to the algo (`ScalpAlgo.on_order_update(...)`)

2) Deliver bar event to the algo:
   `ScalpAlgo.on_bar(bar(t))`

3) Capture broker/account snapshots for equity curve + diagnostics.

Step 2 does NOT implement this loop; it only fixes the project structure
and makes the planned interface explicit.
"""

from typing import Any, Dict, Optional, Sequence


class ReplayRunner(object):
    def __init__(self, api, fleet, friction=None):
        self.api = api
        self.fleet = fleet  # symbol -> ScalpAlgo
        self.friction = friction

    def run(self, start=None, end=None):
        raise NotImplementedError("Step 3 will implement the replay loop.")
