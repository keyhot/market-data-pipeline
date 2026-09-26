"""What the broadcast calls itself (Sprint 17 #27, S16 Track Y T17).

Every broadcast this stream ever created was titled "Market World — live": a
brand nobody has heard of, in the space where the hook should be, never
changing. The title now says what the stream is FOR and carries a number that
is true right now — the model's all-time record of calls, losses included.

Pure: a record dict in, strings out. No client, no clock, no DB. The record
comes from `storage.postgres_store.get_signal_record()` — the ALL-TIME
`signals` table, never `/world/state`, whose record and `first_seen` are a
500-event window. A missing key raises rather than defaults: a confident wrong
figure on the public title is worse than a skipped refresh the runner retries.
"""

TITLE_MAX = 100          # YouTube's limit
DESCRIPTION_MAX = 5000   # YouTube's limit
CATEGORY_ID = "28"       # Science & Technology
TAGS = [
    "algorithmic trading", "AI trading", "live market data", "crypto",
    "bitcoin", "ethereum", "machine learning", "24/7 stream",
]

# Snippet fields YouTube lets a `videos.update` write that this module does
# not own — kept from the current snippet, because part=snippet REPLACES it.
_KEPT = ("defaultLanguage", "defaultAudioLanguage")

_PREMISE = "An AI calls Bitcoin & Ethereum live"


def _n(value: int) -> str:
    return f"{value:,}"


def build_title(record: dict) -> str:
    wins, losses = record["wins"], record["losses"]
    if wins + losses == 0:
        return f"{_PREMISE}, and every call is scored in public"[:TITLE_MAX]
    title = f"{_PREMISE} — {_n(wins)} right, {_n(losses)} wrong, all public"
    if len(title) > TITLE_MAX:
        title = f"{_PREMISE} — {_n(wins)} right, {_n(losses)} wrong"
    return title[:TITLE_MAX]


def build_description(record: dict) -> str:
    wins, losses = record["wins"], record["losses"]
    since = record["since"]
    horizon = record["horizon_bars"]
    total = wins + losses
    lines = [
        "An AI model watches Bitcoin and Ethereum minute by minute and calls "
        f"whether each will be up or down {horizon} minutes later. Every call is "
        "recorded, checked against the real price, and kept — the wrong ones "
        "included.",
        "",
    ]
    if total:
        rate = int(wins * 1000 / total) / 10  # truncated: never rounded up
        lines.append(
            f"Record so far: {_n(wins)} right, {_n(losses)} wrong ({rate:.1f}%), "
            f"across {_n(total)} calls since {since.day} {since:%b %Y}."
        )
    else:
        lines.append("No call has been scored yet.")
    lines += [
        "",
        "A call is right when the price moved the way the model said. This is "
        "a record of calls, not of trading profit.",
        "",
        "Live prices from Binance. Nothing here is financial advice.",
    ]
    return "\n".join(lines)[:DESCRIPTION_MAX]


def build_snippet(record: dict, current: dict) -> dict:
    """The whole `videos.update` snippet: ours, plus the writable fields we
    don't own copied from `current` so the replace doesn't clear them."""
    snippet = {k: current[k] for k in _KEPT if k in current}
    snippet.update(
        title=build_title(record),
        description=build_description(record),
        tags=list(TAGS),
        categoryId=CATEGORY_ID,
    )
    return snippet
