"""
Venue resolution.

Pure: no web framework, no broker, no I/O beyond reading the environment. Kept
out of server.py so it can be tested anywhere — the suites deliberately depend
on nothing outside the standard library.

For four days in September the engine traded Binance testnet while everyone
believed it was on MT5. The venue was set in .env for a hackathon and forgotten.
It was logged once, at startup, and nothing afterwards said where decisions
were executing.

runtime.json is the durable declaration; ZCASTOR_VENUE is an override. When
both are set and disagree, starting anyway is how that happened — so it stops
unless the override is acknowledged explicitly.
"""

from __future__ import annotations

import logging
import os
from typing import Mapping, Optional

logger = logging.getLogger("zcastor.venue")


class VenueDisagreement(RuntimeError):
    """runtime.json and the environment name different venues."""


def resolve_venue(runtime: Mapping, env: Optional[Mapping] = None) -> str:
    env = os.environ if env is None else env
    declared = str(runtime.get("venue", "")).lower()
    override = str(env.get("ZCASTOR_VENUE", "")).lower()

    if declared and override and declared != override:
        if str(env.get("ZCASTOR_VENUE_OVERRIDE", "")).lower() != "yes":
            raise VenueDisagreement(
                f"venue disagreement: runtime.json declares {declared!r} but "
                f"ZCASTOR_VENUE={override!r}. Refusing to start. Fix one, or set "
                f"ZCASTOR_VENUE_OVERRIDE=yes to run on {override!r} deliberately.")
        logger.warning("VENUE OVERRIDE: running %s, runtime.json declares %s",
                       override, declared)

    return override or declared or "mt5"
