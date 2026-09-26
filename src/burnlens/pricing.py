"""API-equivalent pricing, USD per million tokens.

Source: Anthropic first-party list prices (cached 2026-06-24). Subscription
(Pro/Max/Team) users don't pay per token, so costs here are an *equivalent*
figure for comparing sessions, not a bill. Bedrock/Vertex pricing differs.
"""

# model-id prefix -> (input, output, cache_read)
PRICES = {
    "claude-fable-5-1": (10.00, 50.00, 0.25),
    "claude-mythos-5-1": (10.00, 50.00, 0.25),
    "claude-fable-5": (10.00, 50.00, 1.00),
    "claude-mythos-5": (10.00, 50.00, 1.00),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-opus-4": (5.00, 25.00, 0.50),
    "claude-sonnet-5": (2.00, 10.00, 0.20),
    "claude-sonnet-4": (3.00, 15.00, 0.30),
    "claude-haiku-4-5": (1.00, 5.00, 0.10),
}

CACHE_WRITE_5M = 1.25
CACHE_WRITE_1H = 2.0


def rates(model):
    """Longest matching prefix wins, so 'claude-opus-5-5' beats 'claude-opus-5'."""
    if not model:
        return None
    m = model.removeprefix("anthropic.").removeprefix("us.anthropic.")
    best = None
    for prefix in PRICES:
        if m.startswith(prefix) and (best is None or len(prefix) > len(best)):
            best = prefix
    return PRICES[best] if best else None


def cost(model, inp, out, cache_read, cw_5m, cw_1h, fast=False):
    r = rates(model)
    if r is None:
        return None
    i, o, cr = r
    usd = (
        inp * i
        + out * o
        + cache_read * cr
        + cw_5m * i * CACHE_WRITE_5M
        + cw_1h * i * CACHE_WRITE_1H
    ) / 1_000_000
    return usd * 2 if fast else usd
