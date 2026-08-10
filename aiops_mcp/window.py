"""
Time. One module, because there must be exactly one place a string becomes an
instant.

THE BUG THIS FILE EXISTS TO PREVENT
-----------------------------------
Session 2, while re-verifying evidence, parsed the "...Z" strings from
ground_truth.jsonl as LOCAL time and then subtracted the UTC offset, doubling
the error. Every window came back empty, which looked exactly like lost data.
The scenario runner itself never had the bug -- it carries epoch floats end to
end -- so the bug lived only in an ad-hoc script written in a hurry.

An MCP tool is that ad-hoc script, permanently, being called by something that
cannot check the arithmetic. Three structural defences, in order of importance:

1.  A timestamp with no timezone is REJECTED, never assumed. Python's
    fromisoformat() happily returns a naive datetime, and .timestamp() then
    silently interprets it in the host zone -- which on this machine is +05:30.
    That is the exact failure above. Refusing at the door converts a silently
    wrong answer into a loud error.

2.  Every response ECHOES the absolute UTC window that was actually used. If
    something does go wrong, it is visible in the output rather than hidden in
    the arithmetic. This is the cheapest insurance in the whole design: about 25
    tokens.

3.  Out-of-retention windows are CLAMPED WITH A NOTE, never silently emptied.
    Prometheus keeps 24h and Jaeger 72h; asking for three days of metrics is a
    reasonable question with an unreasonable answer, and "no data" would be a
    lie about the system rather than about the store.

WHO PARSES NATURAL LANGUAGE: nobody here.
"15m" is accepted; "the last fifteen minutes" is not. Turning English into 15m
is the model's job and it is good at it. A natural-language date parser is a
dependency and an unbounded silent-error surface -- "last friday" has a
different answer depending on the day it is asked and the zone it is asked in.
A six-character grammar is auditable by reading it.
"""

import re
import time
from datetime import datetime, timedelta, timezone

from . import JAEGER_TTL_HOURS, POINT_BUDGET, PROM_RETENTION_HOURS
from .util import BadArgument

_RELATIVE = re.compile(r"^(\d{1,5})([smhd])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

# Prometheus scrapes every 15s and the OTel agents export every 15s (compose
# sets OTEL_METRIC_EXPORT_INTERVAL=15000 to match -- mismatched intervals make
# staircase-shaped rate() graphs that look like an incident). Asking for a step
# finer than 15s cannot produce new information, only interpolation noise.
SCRAPE_INTERVAL_S = 15


def now_epoch():
    return time.time()


def utc_iso(epoch, millis=False):
    """Epoch -> ISO-8601 UTC with an explicit Z.

    Same format the scenario runner writes into ground_truth.jsonl, so a window
    echoed by a tool can be pasted straight back in as an argument.
    """
    moment = datetime.fromtimestamp(epoch, tz=timezone.utc)
    if millis:
        return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_absolute(text, field):
    """ISO-8601 with a MANDATORY timezone. Naive input is an error, not a guess."""
    if not isinstance(text, str) or not text.strip():
        raise BadArgument(f"{field} must be an ISO-8601 UTC timestamp string",
                          field=field)
    raw = text.strip()

    # fromisoformat handles "+00:00" but only learned "Z" in 3.11. The stack
    # targets 3.12, but normalising costs one line and removes a version
    # dependency from a parser that must not surprise anyone.
    normalised = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        moment = datetime.fromisoformat(normalised)
    except ValueError:
        raise BadArgument(
            f"{field}={raw!r} is not ISO-8601. Expected e.g. 2026-08-09T20:17:31Z",
            field=field)

    if moment.tzinfo is None:
        # The load-bearing rejection. Everything in this stack is UTC;
        # Prometheus and Jaeger are UTC; Windows is not. Assuming a zone here is
        # how an afternoon disappears.
        raise BadArgument(
            f"{field}={raw!r} has no timezone. Timestamps must be UTC with an "
            f"explicit Z, e.g. 2026-08-09T20:17:31Z -- a naive timestamp would "
            f"be interpreted in the server's local zone and silently shift the "
            f"window.",
            field=field)
    return moment.timestamp()


def parse_relative(text, field="window"):
    """'15m' -> 900.0 seconds. Deliberately not English."""
    if not isinstance(text, str):
        raise BadArgument(f"{field} must be a string like '15m'", field=field)
    match = _RELATIVE.match(text.strip())
    if not match:
        raise BadArgument(
            f"{field}={text!r} is not a duration. Use <number><unit> where unit "
            f"is s, m, h or d -- e.g. 15m, 2h, 90s, 1d.",
            field=field)
    amount, unit = int(match.group(1)), match.group(2)
    seconds = amount * _UNIT_SECONDS[unit]
    if seconds <= 0:
        raise BadArgument(f"{field}={text!r} must be greater than zero", field=field)
    return float(seconds)


class Window:
    """A resolved absolute UTC window, plus any notes generated resolving it.

    Notes are part of the answer, not logging. "I clamped your 3-day request to
    24h" changes what the numbers mean, so it travels with them.
    """

    def __init__(self, start, end, notes=None):
        self.start = start
        self.end = end
        self.notes = list(notes or [])

    @property
    def duration(self):
        return self.end - self.start

    def step(self, budget=POINT_BUDGET):
        """Seconds per point, so a series is ~`budget` points WHATEVER the window.

        This is the property that makes the response size independent of the
        question. A 15-minute window and a 24-hour window come back the same
        size; the 24-hour one is simply coarser. The caller cannot make the
        response bigger by asking a bigger question -- which is both the
        context-window defence and the availability defence, and it creates the
        right incentive: to get finer resolution you must ask a narrower
        question, which is the correct thing to do anyway.

        Rounded up to a multiple of the 15s scrape interval, because a step that
        is not a multiple of the scrape interval makes Prometheus interpolate
        between samples and produce points that were never measured.
        """
        raw = max(SCRAPE_INTERVAL_S, self.duration / max(1, budget))
        return int(SCRAPE_INTERVAL_S * max(1, round(raw / SCRAPE_INTERVAL_S)))

    def payload(self):
        return {"start": utc_iso(self.start), "end": utc_iso(self.end),
                "duration_s": int(round(self.duration))}


def resolve(window=None, start=None, end=None, retention_hours=PROM_RETENTION_HOURS,
            store="prometheus", default_window="15m"):
    """Turn the three time arguments into one absolute UTC window.

    Accepts either a relative duration or an absolute pair, never both --
    passing both is an error rather than a silent preference, because a silent
    preference means the caller's intent and the tool's behaviour can disagree
    with nothing to reveal it.
    """
    if window is not None and start is not None:
        raise BadArgument(
            "pass either window (relative, e.g. '15m') or start/end (absolute "
            "UTC), not both", field="window")
    if end is not None and start is None:
        raise BadArgument("end requires start; use window for a relative lookback",
                          field="end")

    notes = []
    now = now_epoch()

    if start is not None:
        start_epoch = parse_absolute(start, "start")
        end_epoch = parse_absolute(end, "end") if end is not None else now
        if end_epoch <= start_epoch:
            raise BadArgument(
                f"end ({utc_iso(end_epoch)}) must be after start "
                f"({utc_iso(start_epoch)})", field="end")
    else:
        seconds = parse_relative(window if window is not None else default_window)
        end_epoch, start_epoch = now, now - seconds

    # --- Retention. Bounded stores make some questions unanswerable, and
    # saying so is the difference between "nothing happened" and "I cannot see
    # that far back". ---------------------------------------------------------
    horizon = now - retention_hours * 3600
    if end_epoch < horizon:
        # Entirely behind the horizon. Clamping would invent a window nobody
        # asked about, so this is a hard error with the real reason attached.
        raise BadArgument(
            f"the requested window ends {utc_iso(end_epoch)}, which is older "
            f"than {store}'s {retention_hours}h retention (nothing before "
            f"{utc_iso(horizon)} exists). This data is gone, not empty.",
            field="start", oldest_available=utc_iso(horizon))
    if start_epoch < horizon:
        notes.append(
            f"requested start {utc_iso(start_epoch)} is beyond {store}'s "
            f"{retention_hours}h retention; clamped to {utc_iso(horizon)}.")
        start_epoch = horizon

    # A window ending in the future is not an error -- a clock can be a second
    # off and rounding is common -- but the numbers are only as fresh as the
    # last scrape, so anything substantial is worth saying out loud.
    if end_epoch > now + 60:
        notes.append(
            f"requested end {utc_iso(end_epoch)} is in the future; no data "
            f"exists after {utc_iso(now)}.")

    if end_epoch - start_epoch < SCRAPE_INTERVAL_S:
        raise BadArgument(
            f"window is {end_epoch - start_epoch:.0f}s, shorter than the "
            f"{SCRAPE_INTERVAL_S}s scrape interval -- it could not contain even "
            f"one sample. Use at least 1m.", field="window")

    return Window(start_epoch, end_epoch, notes)


def jaeger_window(window=None, start=None, end=None):
    """Same resolution against Jaeger's 72h Badger TTL instead of Prometheus's 24h."""
    return resolve(window=window, start=start, end=end,
                   retention_hours=JAEGER_TTL_HOURS, store="jaeger")
