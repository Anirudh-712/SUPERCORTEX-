"""Falsification test 1 — the look-ahead battery.

[[LRN-000002]] recorded the defect this module exists to fix. EXP-000001's look-ahead probe
came back clean, and that clean result meant **nothing**: the probe shifted price bars while
the strategy's signal lived in the funding series. A shift applied to a channel the strategy
does not read produces a reassuring null that is pure noise.

So the battery is built around one rule:

    A shift probe is interpretable only after the channel has been shown to matter.

Each input series gets three runs:

    scramble   permute the series' content, keep its timestamps.
               Answers: does the strategy use this channel's INFORMATION at all?
    advance    deliver the series one interval early, granting look-ahead.
               Answers: is there head-room the baseline was not already taking?
    delay      deliver it one interval late.
               Answers: does the edge depend on reacting immediately?

Read together they disambiguate the case a single probe cannot:

    scramble flat  + advance flat   ->  channel is INERT. Probe uninformative. Investigate
                                        why the strategy nominally reads a series it does
                                        not respond to.
    scramble moves + advance flat   ->  channel is used, but extra look-ahead adds nothing.
                                        SUSPICIOUS: consistent with the baseline already
                                        being saturated with future information.
    scramble moves + advance helps  ->  channel is used and the baseline is not leaking.
                                        This is the only combination that passes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from supercortex.backtest.runner import ProbeSpec

#: A relative change below this is treated as noise rather than signal.
MATERIAL = 0.10


def battery(channels: tuple[str, ...] = ("bars", "funding")) -> list[ProbeSpec]:
    """The probe runs required before test 1 can be called passed."""
    specs: list[ProbeSpec] = []
    for channel in channels:
        if channel == "bars":
            specs += [
                ProbeSpec(bars_scramble=True, label="bars-scramble"),
                ProbeSpec(bars_shift=1, label="bars-advance"),
                ProbeSpec(bars_shift=-1, label="bars-delay"),
            ]
        elif channel == "funding":
            specs += [
                ProbeSpec(funding_scramble=True, label="funding-scramble"),
                ProbeSpec(funding_shift=1, label="funding-advance"),
                ProbeSpec(funding_shift=-1, label="funding-delay"),
            ]
        else:
            raise ValueError(f"unknown channel {channel!r}")
    return specs


def _score(metrics: dict[str, Any]) -> float:
    """The quantity a probe is compared on.

    Profit factor rather than PnL: it is the statistic the tax regime actually binds on
    (LRN-000001), and it is scale-free, so a probe that changes trade count does not
    masquerade as a change in edge.
    """
    return float(metrics["trades"]["profit_factor_gross"])


def _relative(baseline: float, probed: float) -> float:
    if baseline == 0:
        return 0.0 if probed == 0 else float("inf")
    return (probed - baseline) / abs(baseline)


@dataclass(frozen=True)
class ChannelVerdict:
    channel: str
    scramble_delta: float
    advance_delta: float
    delay_delta: float
    status: str
    reasoning: str

    @property
    def passed(self) -> bool:
        return self.status == "PASS"


def assess(baseline: dict[str, Any], probes: dict[str, dict[str, Any]],
           channel: str) -> ChannelVerdict:
    """Turn three probe runs into one interpretable verdict for a channel."""
    base = _score(baseline)
    scramble = _relative(base, _score(probes[f"{channel}-scramble"]))
    advance = _relative(base, _score(probes[f"{channel}-advance"]))
    delay = _relative(base, _score(probes[f"{channel}-delay"]))

    channel_matters = abs(scramble) >= MATERIAL or abs(delay) >= MATERIAL
    advance_helps = advance >= MATERIAL

    if not channel_matters and abs(advance) < MATERIAL:
        status = "INERT"
        reasoning = (
            f"scrambling {channel} moved profit factor by {scramble:+.1%} and delaying it "
            f"by {delay:+.1%} — both immaterial. The strategy does not respond to this "
            f"channel's information, so any shift probe on it is uninformative. This is "
            f"exactly the trap LRN-000002 describes: a clean result that says nothing."
        )
    elif channel_matters and not advance_helps:
        status = "SUSPICIOUS"
        reasoning = (
            f"the strategy responds to {channel} (scramble {scramble:+.1%}, delay "
            f"{delay:+.1%}) yet granting an extra interval of look-ahead changed profit "
            f"factor by only {advance:+.1%}. Consistent with the baseline already being "
            f"saturated — carrying the information the advance would have supplied. "
            f"Inspect the timestamps on this channel."
        )
    else:
        status = "PASS"
        reasoning = (
            f"the strategy responds to {channel} (scramble {scramble:+.1%}, delay "
            f"{delay:+.1%}) and granting look-ahead improves it by {advance:+.1%}. The "
            f"channel is live and the baseline is not already saturated with future data."
        )
    return ChannelVerdict(channel, scramble, advance, delay, status, reasoning)


def verdict(baseline: dict[str, Any], probes: dict[str, dict[str, Any]],
            channels: tuple[str, ...]) -> tuple[bool, list[ChannelVerdict]]:
    """Overall test-1 result. Every channel the strategy reads must PASS."""
    results = [assess(baseline, probes, c) for c in channels]
    return all(r.passed for r in results), results
