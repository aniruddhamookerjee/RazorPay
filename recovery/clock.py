"""Virtual clock, so a 7-day recovery window replays in seconds.

Recovery windows are 7 days; the demo is five minutes. The simulation therefore
runs on a clock we advance ourselves rather than on wall time.

Two timestamps are kept on every record (see `recovery/models.py`):

    virtual_time  when it happened in the simulated world
    created_at    when the process actually did it

The live subset executes against real Razorpay APIs in real time and carries
both — real for the API call, virtual so it sits on the same timeline as the
simulated batch and the two can be compared.

The clock is deliberately not global. An experiment arm gets its own instance,
so arms cannot leak time into each other, and a replay of the same seed produces
the same timeline every run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# Fixed simulation epoch. A hard-coded start makes runs comparable across
# machines and across days — "day 3 of the window" means the same thing in
# every report, and nothing shifts because someone re-ran it on a Tuesday.
EPOCH = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)


@dataclass
class VirtualClock:
    """A monotonic clock the simulation advances by hand.

    Time only ever moves forward. `advance_to` on a past instant is a bug in the
    scheduler, not something to silently absorb, so it raises.
    """

    now: datetime = EPOCH
    _events: int = field(default=0, repr=False)

    def advance(self, *, days: int = 0, hours: int = 0, minutes: int = 0) -> datetime:
        """Move forward by a duration and return the new time."""
        delta = timedelta(days=days, hours=hours, minutes=minutes)
        if delta < timedelta(0):
            raise ValueError("VirtualClock only moves forward")
        self.now += delta
        self._events += 1
        return self.now

    def advance_to(self, instant: datetime) -> datetime:
        """Jump to an absolute instant, which must not be in the past."""
        if instant < self.now:
            raise ValueError(
                f"cannot rewind: {instant.isoformat()} is before {self.now.isoformat()}"
            )
        self.now = instant
        self._events += 1
        return self.now

    def at(self, *, days: int = 0, hours: int = 0, minutes: int = 0) -> datetime:
        """A future instant *without* moving the clock — for scheduling."""
        return self.now + timedelta(days=days, hours=hours, minutes=minutes)

    def elapsed_since(self, instant: datetime) -> timedelta:
        return self.now - instant

    def within_window(self, start: datetime, *, days: int) -> bool:
        """Is the clock still inside a recovery window opened at `start`?

        The boundary is exclusive: a charge settling exactly at day 7 is
        outside. `definitions.md` §1 makes the window a policy choice, so the
        boundary has to be stated somewhere and enforced in one place — here.
        """
        return self.now < start + timedelta(days=days)

    def reset(self, to: datetime = EPOCH) -> None:
        self.now = to
        self._events = 0
