"""Deterministic per-character, per-event-type, per-tier phrase banks (Sprint 13).

No LLM, no API cost. ``PHRASES[character][event_type][tier]`` is a list of
``str.format`` templates rendered against the event payload — so any number a
character quotes comes from the real event, never invented (the truthfulness
invariant). Only ``big_move`` ({sigmas}) and ``streak`` ({bars}/{direction})
interpolate numbers; the rest are number-free and truthful by omission.

Tier keys are the *minimum* tier at which the character speaks; ``line_for``
falls back to the highest available tier ≤ the requested one, so keep each
bank's tiers contiguous (no gaps). An empty ``{}`` means the character ignores
that event type entirely — the key stays present for the registry invariant.

Register: quiet by default. The optimist celebrates and shrugs off losses; the
statistician only speaks at a high tier; the anxious one frets over volatility
and losses (kept sparingly loud via a high min_tier in personalities.py).
"""

PHRASES: dict[str, dict[str, dict[int, list[str]]]] = {
    "optimist": {
        "big_move": {
            1: [
                "{sigmas:.0f}σ on {symbol} — now we're moving!",
                "There it goes! A {sigmas:.0f} sigma push on {symbol}.",
            ]
        },
        "volatility_spike": {
            1: [
                "Things are waking up on {symbol}.",
                "A little turbulence — nothing we can't ride.",
            ]
        },
        "gap_open": {1: ["{symbol} gapped — fresh start, fresh chances."]},
        "volume_anomaly": {1: ["Volume's pouring into {symbol} — people care!"]},
        "streak": {
            1: [
                "{bars} closes {direction} on {symbol} — momentum!",
                "{bars} in a row {direction}. I love a good run.",
            ]
        },
        "signal_resolved": {
            1: ["Called it — another one right!", "{symbol} came through. Nice."]
        },
        "model_losing_streak": {},  # the optimist shrugs off cold runs
        "stream_started": {0: ["We're live! Welcome in, everyone."]},
        "stream_stopped": {0: ["That's a wrap for now — thanks for watching."]},
        "stream_dropped": {},
        "trader_opened": {0: ["The trader's in on {symbol} — bold move!"]},
        "trader_closed": {0: ["Position closed. On to the next one."]},
        "trader_milestone": {0: ["Milestone reached — look at that go!"]},
    },
    "statistician": {
        "big_move": {
            2: ["{sigmas:.1f}σ on {symbol}. Notable deviation."],
            3: ["{sigmas:.1f}σ — not noise, a genuine tail on {symbol}."],
        },
        "volatility_spike": {2: ["Regime shift in {symbol} volatility."]},
        "gap_open": {},
        "volume_anomaly": {2: ["{symbol} volume is well outside its distribution."]},
        "streak": {2: ["{bars} {direction} closes in a row. Runs test objects."]},
        "signal_resolved": {
            2: ["Resolved {outcome}. The sample grows."],
            3: ["A high-conviction call resolved {outcome}. That moves the mean."],
        },
        "model_losing_streak": {
            2: ["A cold run. Regression to the mean is not a strategy."],
            3: ["A significant losing streak — the edge, if any, isn't showing."],
        },
        "stream_started": {},
        "stream_stopped": {},
        "stream_dropped": {},
        "trader_opened": {},
        "trader_closed": {2: ["Trade closed — one more realized data point."]},
        "trader_milestone": {},
    },
    "anxious": {
        "big_move": {2: ["A {sigmas:.0f}σ lurch on {symbol}. I don't like it."]},
        "volatility_spike": {
            1: ["It's getting choppy on {symbol}…", "Volatility's climbing. Hold on."],
            2: ["This is really shaking now — {symbol} won't sit still."],
        },
        "gap_open": {
            1: ["{symbol} gapped overnight — that's a gap to watch."],
            2: ["A big gap on {symbol}. Gaps make me nervous."],
        },
        "volume_anomaly": {2: ["Volume like this in {symbol} isn't nothing…"]},
        "streak": {
            1: [
                "{bars} {direction} in a row on {symbol}… streaks don't last.",
                "{bars} closes {direction}. It'll snap back, won't it?",
            ]
        },
        "signal_resolved": {
            1: ["We got that one wrong, didn't we.", "Another one didn't land."],
            2: ["A confident call, wrong. This is how it starts."],
        },
        "model_losing_streak": {
            1: ["The losses are stacking up. I said this would happen."],
            2: ["A losing streak — I really don't like where this is going."],
        },
        "stream_started": {},
        "stream_stopped": {0: ["We went dark… I hope everything's okay."]},
        "stream_dropped": {1: ["We're dropping frames — is the stream alright?"]},
        "trader_opened": {
            1: ["The trader opened {symbol}. Do they know what they're doing?"]
        },
        "trader_closed": {1: ["Position closed. Was that the right call?"]},
        "trader_milestone": {},
    },
}
