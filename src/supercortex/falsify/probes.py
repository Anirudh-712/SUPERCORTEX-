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
from enum import StrEnum
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


def score(metrics: dict[str, Any]) -> float | None:
    """The quantity a probe is compared on, or None when no comparison exists.

    Profit factor rather than PnL: it is the statistic the tax regime actually binds on
    (LRN-000001), and it is scale-free, so a probe that changes trade count does not
    masquerade as a change in edge.

    An unbounded or undefined profit factor has no finite value, so the *relative* change
    a probe is judged on cannot be computed. That returns None, and the caller refuses —
    a probe that cannot be compared has not been passed.

    Documents sealed before the state tag existed carry a bare number, and a bare number
    was always finite: nothing else could be written into strict JSON. Reading them that
    way is history, not inference.
    """
    trades = metrics["trades"]
    value = trades.get("profit_factor_gross")
    state = trades.get("profit_factor_gross_state", "finite")
    if value is None or state != "finite":
        return None
    return float(value)


def _relative(baseline: float, probed: float) -> float:
    if baseline == 0:
        return 0.0 if probed == 0 else float("inf")
    return (probed - baseline) / abs(baseline)


class ChannelStatus(StrEnum):
    """What three probe runs can say about one input channel.

    Four members because the three runs genuinely disambiguate four situations, and
    collapsing them loses the distinction the battery was built to make: INERT means the
    probe was uninformative, UNDEFINED means no comparison exists, SUSPICIOUS means the
    channel is used but extra look-ahead bought nothing. None of those is a FAIL.
    """

    PASS = "PASS"
    SUSPICIOUS = "SUSPICIOUS"
    INERT = "INERT"
    UNDEFINED = "UNDEFINED"


@dataclass(frozen=True)
class ChannelVerdict:
    channel: str
    #: None where the comparison is undefined — see `score`.
    scramble_delta: float | None
    advance_delta: float | None
    delay_delta: float | None
    status: ChannelStatus
    reasoning: str

    @property
    def passed(self) -> bool:
        return self.status is ChannelStatus.PASS


def assess(baseline: dict[str, Any], probes: dict[str, dict[str, Any]],
           channel: str) -> ChannelVerdict:
    """Turn three probe runs into one interpretable verdict for a channel."""
    base = score(baseline)
    scrambled = score(probes[f"{channel}-scramble"])
    advanced = score(probes[f"{channel}-advance"])
    delayed = score(probes[f"{channel}-delay"])

    if base is None or scrambled is None or advanced is None or delayed is None:
        absent = (["the baseline"] if base is None else []) + [
            f"{channel}-{name}"
            for name, value in (("scramble", scrambled), ("advance", advanced),
                                ("delay", delayed))
            if value is None
        ]
        return ChannelVerdict(
            channel, None, None, None, ChannelStatus.UNDEFINED,
            f"profit factor is undefined for {', '.join(absent)}, so the relative change "
            f"a probe is judged on does not exist. Cannot compare, therefore not a pass — "
            f"an all-winning or zero-trade run is a real result, but it is not a "
            f"measurement of how this channel responds.",
        )

    scramble = _relative(base, scrambled)
    advance = _relative(base, advanced)
    delay = _relative(base, delayed)

    channel_matters = abs(scramble) >= MATERIAL or abs(delay) >= MATERIAL
    advance_helps = advance >= MATERIAL

    if not channel_matters and abs(advance) < MATERIAL:
        status = ChannelStatus.INERT
        reasoning = (
            f"scrambling {channel} moved profit factor by {scramble:+.1%} and delaying it "
            f"by {delay:+.1%} — both immaterial. The strategy does not respond to this "
            f"channel's information, so any shift probe on it is uninformative. This is "
            f"exactly the trap LRN-000002 describes: a clean result that says nothing."
        )
    elif channel_matters and not advance_helps:
        status = ChannelStatus.SUSPICIOUS
        reasoning = (
            f"the strategy responds to {channel} (scramble {scramble:+.1%}, delay "
            f"{delay:+.1%}) yet granting an extra interval of look-ahead changed profit "
            f"factor by only {advance:+.1%}. Consistent with the baseline already being "
            f"saturated — carrying the information the advance would have supplied. "
            f"Inspect the timestamps on this channel."
        )
    else:
        status = ChannelStatus.PASS
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
