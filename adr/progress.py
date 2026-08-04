"""How fast is this going, and how much longer?

The encode phase has answered that since the beginning, because HandBrake says
so itself. The rip never has — and the rip is the long one. A forty-minute
Blu-ray showed a percentage and nothing else, which tells you where you are but
not whether to wait or come back after dinner, and cannot distinguish a slow
disc from a stuck one.

MakeMKV reports a position, not a rate, so the rate has to be derived. Two
decisions matter more than the arithmetic:

**Recent, not average.** A rip begins with a scan and a spin-up during which
progress barely moves. Averaged over the whole run, that early stall drags the
estimate up for the rest of it. The rate is measured across a moving window
instead, so the number reflects what the drive is doing now.

**Silence beats a wrong number.** For the first few seconds there is not enough
information to say anything, and "4 hours remaining" on a disc that finishes in
twenty minutes is worse than no estimate at all — someone walks away on the
strength of it. Nothing is reported until the samples support it.
"""

from __future__ import annotations

import time
from collections import deque

#: How far back the rate is measured. Long enough to smooth out the pauses
#: MakeMKV takes between titles, short enough to follow a drive that slows
#: down on a damaged patch.
DEFAULT_WINDOW = 60.0

#: Below this many samples, or this many seconds, no estimate is offered.
DEFAULT_MIN_SAMPLES = 3
DEFAULT_MIN_ELAPSED = 8.0


class Rate:
    """The rate of change of a value that only goes up.

    Used twice: over the progress fraction, to estimate time remaining, and
    over bytes written, to report a speed. Both are the same question asked of
    a different number.
    """

    def __init__(
        self,
        window: float = DEFAULT_WINDOW,
        min_samples: int = DEFAULT_MIN_SAMPLES,
        min_elapsed: float = DEFAULT_MIN_ELAPSED,
    ):
        self._window = window
        self._min_samples = min_samples
        self._min_elapsed = min_elapsed
        self._samples: deque[tuple[float, float]] = deque()

    def reset(self) -> None:
        """Forget everything measured so far."""
        self._samples.clear()

    def update(self, value: float, now: float | None = None) -> None:
        """Record *value* as of *now*.

        A value that has gone backwards means the thing being measured started
        over — a new title, a retry, a fresh phase. Carrying samples across
        that would produce a negative rate and an ETA in the past, so the
        history is dropped and measurement begins again.
        """
        now = time.monotonic() if now is None else now
        if self._samples and value < self._samples[-1][1]:
            self._samples.clear()
        self._samples.append((now, float(value)))
        cutoff = now - self._window
        while len(self._samples) > 2 and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def per_second(self) -> float | None:
        """Units per second across the window, or None if it cannot be said."""
        if len(self._samples) < self._min_samples:
            return None
        (first_at, first_value), (last_at, last_value) = self._samples[0], self._samples[-1]
        elapsed = last_at - first_at
        if elapsed < self._min_elapsed:
            return None
        gained = last_value - first_value
        if gained <= 0:
            return None
        return gained / elapsed

    def eta_to(self, target: float = 1.0) -> int | None:
        """Seconds until the value reaches *target*, or None.

        None means "not enough information", never "no time left" — a caller
        showing this must say nothing rather than imply the work is done.
        """
        rate = self.per_second()
        if rate is None:
            return None
        remaining = target - self._samples[-1][1]
        if remaining <= 0:
            return 0
        return int(remaining / rate)


def format_eta(seconds: int | None) -> str:
    """A human duration: '3h 20m', '12m 30s', '45s'. Empty for None.

    Deliberately two units at most. "2h 13m 44s" implies a precision an
    estimate does not have, and the seconds are stale by the time they are
    read.
    """
    if seconds is None or seconds < 0:
        return ""
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if minutes else f"{hours}h"


def format_speed(bytes_per_second: float | None) -> str:
    """A human transfer rate: '8.4 MB/s'. Empty for None.

    A drive crawling along re-reading a damaged sector does single-digit
    kilobytes, and rounding that to '0 KB/s' would hide the very thing worth
    seeing, so the small end keeps its units.
    """
    if not bytes_per_second or bytes_per_second <= 0:
        return ""
    if bytes_per_second < 1024:
        return f"{bytes_per_second:.0f} B/s"
    kb = bytes_per_second / 1024
    if kb < 1024:
        return f"{kb:.0f} KB/s"
    mb = kb / 1024
    return f"{mb:.0f} MB/s" if mb >= 10 else f"{mb:.1f} MB/s"


def directory_size(path) -> int:
    """Total bytes of the files directly inside *path*. Zero if unreadable.

    Used to measure how fast a rip is actually writing. The rip's own progress
    is a percentage of an unknown total, so it cannot answer "is this drive
    reading at 8 MB/s or 0.2" — which is the number that tells a healthy disc
    from one being re-read sector by sector.
    """
    import os

    total = 0
    try:
        with os.scandir(str(path)) as entries:
            for entry in entries:
                try:
                    if entry.is_file():
                        total += entry.stat().st_size
                except OSError:
                    continue
    except OSError:
        return 0
    return total
