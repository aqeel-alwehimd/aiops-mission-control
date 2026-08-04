"""
replay.py -- the virtual clock.

There is no live cluster. The demo replays the historical test period through a compressible
clock: every prediction is the real model's real output on real data; only *time* is simulated.

Deploy-safe design: the DEFAULT position is derived purely from wall-clock time since a fixed
epoch, looping through the replay window. So any process/worker computes the same virtual time,
and a cold-started (slept-then-woken) free-tier instance shows a sensible, live moment rather
than snapping back to the window start. Pause / speed / step / jump are an in-memory manual
override layered on top of that base; they are per-process and simply fall away on a cold start,
returning the clock to the shared, wall-clock-derived "live" position.
"""
import time, threading, datetime

# A fixed real-time reference so every process maps wall-clock -> virtual time identically.
BASE_EPOCH = 1_700_000_000        # 2023-11-14T22:13:20Z, arbitrary but fixed
BASE_SPEED = 3600.0               # default virtual seconds per real second (window loops)


class VirtualClock:
    def __init__(self, window_start_ts: int, window_end_ts: int, speed: float = BASE_SPEED):
        self.win_start = int(window_start_ts)
        self.win_end = int(window_end_ts)
        self.span = max(1, self.win_end - self.win_start)
        self.base_speed = float(speed)
        self._manual = None        # None -> stateless wall-clock base; else dict(vt, anchor, paused, speed)
        self._lock = threading.Lock()

    # ---- the stateless default: position is a pure function of wall-clock time ----
    def _base_vt(self, now=None) -> float:
        now = time.time() if now is None else now
        elapsed = (now - BASE_EPOCH) * self.base_speed
        return self.win_start + (elapsed % self.span)          # loops through the window forever

    # ---- advance a manual override to 'now' (no-op in stateless mode) ----
    def _sync(self):
        m = self._manual
        if m is None:
            return
        now = time.time()
        if not m["paused"]:
            m["vt"] += (now - m["anchor"]) * m["speed"]
            if m["vt"] >= self.win_end:                        # reached the end -> hold and pause
                m["vt"] = float(self.win_end); m["paused"] = True
            elif m["vt"] < self.win_start:
                m["vt"] = float(self.win_start)
        m["anchor"] = now

    def _ensure_manual(self):
        """Switch from the stateless base into a manual override, seeded at the current position."""
        if self._manual is None:
            self._manual = {"vt": float(self._base_vt()), "anchor": time.time(),
                            "paused": False, "speed": self.base_speed}

    def now_ts(self) -> int:
        with self._lock:
            if self._manual is None:
                return int(self._base_vt())
            self._sync()
            return int(self._manual["vt"])

    # ---- controls: offsets layered on top of the stateless base ----
    def play(self):
        with self._lock:
            self._ensure_manual(); self._sync(); self._manual["paused"] = False

    def pause(self):
        with self._lock:
            self._ensure_manual(); self._sync(); self._manual["paused"] = True

    def set_speed(self, speed: float):
        with self._lock:
            self._ensure_manual(); self._sync(); self._manual["speed"] = max(1.0, float(speed))

    def step(self, seconds: float):
        """Nudge the virtual clock by N virtual seconds (works while paused)."""
        with self._lock:
            self._ensure_manual(); self._sync()
            self._manual["vt"] = max(self.win_start, min(self.win_end, self._manual["vt"] + float(seconds)))

    def jump(self, virtual_ts: float):
        with self._lock:
            self._ensure_manual(); self._sync()
            self._manual["vt"] = max(self.win_start, min(self.win_end, float(virtual_ts)))

    def goto(self, virtual_ts: float):
        """Land on an exact virtual moment AND HOLD there. The one control a demo needs.

        WHY THIS EXISTS, in arithmetic. The report cache is keyed on the virtual time bucketed to
        CACHE_BUCKET (900 virtual seconds). At the default 3600x, the live clock crosses the whole
        72-hour replay window in 72 real seconds, so each 15-minute bucket is the current one for
        0.25 REAL SECONDS. A report prepared for a chosen moment is therefore reachable roughly
        once every 72 seconds, for a quarter of a second -- which is not something anyone can
        demonstrate.

        `jump` alone does not fix it: _ensure_manual() seeds the override with paused=False, so a
        jump starts a manual clock that immediately runs at 3600x and leaves the bucket in the same
        quarter second. Pausing separately is a second HTTP call with the clock running in between.
        This does both inside one acquisition of the lock, so the landing is exact.

        Nothing else changes. play / pause / step / speed / jump / jump_frac keep their behaviour,
        and reset() still drops the override and returns to the shared wall-clock position, so
        normal operation is untouched and a demo pin cannot outlive a restart.
        """
        with self._lock:
            self._ensure_manual(); self._sync()
            self._manual["vt"] = max(self.win_start, min(self.win_end, float(virtual_ts)))
            self._manual["paused"] = True

    def jump_frac(self, frac: float):
        """Jump to a fraction (0..1) through the replay window."""
        f = max(0.0, min(1.0, float(frac)))
        self.jump(self.win_start + f * self.span)

    def reset(self):
        """Drop the manual override -> return to the stateless, wall-clock-derived 'live' position."""
        with self._lock:
            self._manual = None

    def state(self) -> dict:
        with self._lock:
            if self._manual is None:
                vt = self._base_vt(); paused = False; speed = self.base_speed; live = True
            else:
                self._sync()
                vt = self._manual["vt"]; paused = self._manual["paused"]; speed = self._manual["speed"]; live = False
            return {
                "virtual_ts": int(vt),
                "virtual_iso": _iso(vt),
                "paused": paused,
                "speed": speed,
                "live": live,                       # True when following the shared wall-clock base
                "window_start_ts": self.win_start,
                "window_end_ts": self.win_end,
                "window_start_iso": _iso(self.win_start),
                "window_end_iso": _iso(self.win_end),
                "progress": round((vt - self.win_start) / self.span, 4),
            }


def _iso(ts) -> str:
    return datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S") + "Z"
