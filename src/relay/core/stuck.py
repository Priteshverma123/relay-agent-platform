"""Stuck / loop detection over a sliding window of (action, observation) signatures.

"Agent stuck in loop" with no recovery is a top complaint in OpenHands' issue tracker, so
detection is paired with an escalating recovery ladder:
    1st signal → RE_PLAN   (inject a reflection message telling the agent what it's repeating)
    2nd signal → ASK_HUMAN (approval interrupt: human can give a hint or abort)
    3rd signal → ABORT     (finish with a partial result and the reason)
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import StrEnum

from relay.common.text import canonical_hash


class StuckKind(StrEnum):
    REPEATED_ACTION = "repeated_action"  # same call + same args N times
    REPEATED_ERROR = "repeated_error"  # different calls, same error output N times
    PING_PONG = "ping_pong"  # A,B,A,B,A,B
    NO_PROGRESS = "no_progress"  # N consecutive empty/no-op observations


class Recovery(StrEnum):
    RE_PLAN = "re_plan"
    ASK_HUMAN = "ask_human"
    ABORT = "abort"


@dataclass(slots=True, frozen=True)
class StuckSignal:
    kind: StuckKind
    detail: str
    recovery: Recovery


class StuckDetector:
    def __init__(self, window: int = 12, repeat: int = 3, pingpong_cycles: int = 3, no_progress: int = 6) -> None:
        self.hist: deque[tuple[str, str, bool, bool]] = deque(maxlen=window)
        self.repeat, self.cycles, self.no_progress = repeat, pingpong_cycles, no_progress
        self.signals = 0

    def observe(self, tool: str, args: dict[str, object], output: str, *, is_error: bool) -> StuckSignal | None:
        action = canonical_hash(tool, args)[:16]
        obs = canonical_hash(output.strip()[-2000:])[:16]
        empty = not output.strip() or "produced no output" in output
        self.hist.append((action, obs, is_error, empty))
        kind, detail = self._check(tool)
        if kind is None:
            return None
        self.signals += 1
        ladder = [Recovery.RE_PLAN, Recovery.ASK_HUMAN, Recovery.ABORT]
        sig = StuckSignal(kind, detail, ladder[min(self.signals, 3) - 1])
        self.hist.clear()  # give the recovery a fresh window
        return sig

    def _check(self, tool: str) -> tuple[StuckKind | None, str]:
        h = list(self.hist)
        if len(h) >= self.repeat and len({(a, o) for a, o, _, _ in h[-self.repeat:]}) == 1:
            return StuckKind.REPEATED_ACTION, f"'{tool}' called {self.repeat}x with identical args and result"
        errs = [o for _, o, e, _ in h[-self.repeat:] if e]
        if len(errs) == self.repeat and len(set(errs)) == 1:
            return StuckKind.REPEATED_ERROR, f"the same error occurred {self.repeat}x in a row"
        n = 2 * self.cycles
        if len(h) >= n:
            tail = [a for a, _, _, _ in h[-n:]]
            if tail[0] != tail[1] and all(tail[i] == tail[i % 2] for i in range(n)):
                return StuckKind.PING_PONG, f"alternating between two actions for {self.cycles} cycles"
        if len(h) >= self.no_progress and all(emp for *_, emp in h[-self.no_progress:]):
            return StuckKind.NO_PROGRESS, f"{self.no_progress} consecutive actions produced no output"
        return None, ""

    @staticmethod
    def reflection(sig: StuckSignal) -> str:
        return (f"You appear to be stuck ({sig.kind.value}: {sig.detail}). Stop repeating that approach. "
                "Re-read the task, state what you have learned so far in one paragraph, then choose a "
                "different strategy. If the task cannot be completed, finish and explain why.")
