"""Pricing helpers for normalized attempt cost reporting.

Pricing is based on the public API pricing pages as of 2026-04-17:
- OpenAI: https://openai.com/api/pricing/
- Anthropic: https://platform.claude.com/docs/en/about-claude/pricing
- Fireworks: https://fireworks.ai/pricing

When a CLI does not emit an explicit cost, RepoGauge falls back to these token
rates so analysis can still report approximate spend. These estimates are best
effort and may omit vendor-specific runtime/tool surcharges that are not
represented in the usage payload.
"""

from __future__ import annotations

from typing import Any, Mapping


MODEL_TOKEN_PRICING_USD_PER_MILLION: dict[str, dict[str, float]] = {
    # OpenAI standard pricing.
    "gpt-5.4": {"input": 2.50, "cached_input": 0.25, "output": 15.00},
    "gpt-5.4-mini": {"input": 0.75, "cached_input": 0.075, "output": 4.50},
    "gpt-5.4-nano": {"input": 0.20, "cached_input": 0.02, "output": 1.25},
    "gpt-5": {"input": 1.25, "cached_input": 0.125, "output": 10.00},
    "gpt-5-mini": {"input": 0.25, "cached_input": 0.025, "output": 2.00},
    "gpt-5-nano": {"input": 0.05, "cached_input": 0.005, "output": 0.40},
    # Anthropic standard pricing.
    "claude-opus-4.7": {
        "input": 5.00,
        "cache_read": 0.50,
        "cache_write_5m": 6.25,
        "cache_write_1h": 10.00,
        "output": 25.00,
    },
    "claude-opus-4.6": {
        "input": 5.00,
        "cache_read": 0.50,
        "cache_write_5m": 6.25,
        "cache_write_1h": 10.00,
        "output": 25.00,
    },
    "claude-opus-4.5": {
        "input": 5.00,
        "cache_read": 0.50,
        "cache_write_5m": 6.25,
        "cache_write_1h": 10.00,
        "output": 25.00,
    },
    "claude-sonnet-4.6": {
        "input": 3.00,
        "cache_read": 0.30,
        "cache_write_5m": 3.75,
        "cache_write_1h": 6.00,
        "output": 15.00,
    },
    "claude-sonnet-4.5": {
        "input": 3.00,
        "cache_read": 0.30,
        "cache_write_5m": 3.75,
        "cache_write_1h": 6.00,
        "output": 15.00,
    },
    "claude-sonnet-4": {
        "input": 3.00,
        "cache_read": 0.30,
        "cache_write_5m": 3.75,
        "cache_write_1h": 6.00,
        "output": 15.00,
    },
    "claude-haiku-4.5": {
        "input": 1.00,
        "cache_read": 0.10,
        "cache_write_5m": 1.25,
        "cache_write_1h": 2.00,
        "output": 5.00,
    },
    # Fireworks standard pricing.
    "kimi-k2p5": {"input": 0.60, "cached_input": 0.10, "output": 3.00},
    "kimi-k2p5-turbo": {"input": 0.99, "cached_input": 0.16, "output": 4.94},
}


_MODEL_ALIASES = {
    "claude-opus-4-7": "claude-opus-4.7",
    "claude-opus-4-6": "claude-opus-4.6",
    "claude-opus-4-5": "claude-opus-4.5",
    "claude-sonnet-4-6": "claude-sonnet-4.6",
    "claude-sonnet-4-5": "claude-sonnet-4.5",
    "claude-haiku-4-5": "claude-haiku-4.5",
    "kimi-k2.5": "kimi-k2p5",
    "kimi-k2.5-turbo": "kimi-k2p5-turbo",
}


def _coerce_non_negative_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _coerce_non_negative_float(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if parsed > 0 else 0.0


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _int_from_candidates(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> int:
    for key in keys:
        if key in mapping:
            return _coerce_non_negative_int(mapping.get(key))
    return 0


def normalize_model_name(value: Any) -> str:
    normalized = str(value or "").strip().lower().replace("_", "-")
    candidates = [normalized]
    if "/" in normalized:
        candidates.append(normalized.rsplit("/", 1)[-1])
    if "models/" in normalized:
        candidates.append(normalized.split("models/", 1)[-1])
    if "accounts/" in normalized:
        candidates.append(normalized.split("accounts/", 1)[-1])

    for candidate in candidates:
        aliased = _MODEL_ALIASES.get(candidate, candidate)
        if aliased in MODEL_TOKEN_PRICING_USD_PER_MILLION:
            return aliased

    return _MODEL_ALIASES.get(normalized, normalized)


def read_cost_usd(value: Any) -> float | None:
    """Read a cost payload and return a normalized USD total when possible."""
    cost = _mapping(value)
    if not cost:
        return None

    for key in ("total_cost_usd", "total_cost", "usd", "value", "amount", "total_usd"):
        if key in cost:
            amount = _coerce_non_negative_float(cost.get(key))
            if amount > 0:
                return amount

    component_total = 0.0
    component_keys = (
        "input_cost",
        "output_cost",
        "cached_input_cost",
        "cache_read_cost",
        "cache_creation_cost",
        "cache_write_cost",
        "cache_write_5m_cost",
        "cache_write_1h_cost",
        "tool_cost",
        "server_tool_cost",
    )
    matched = False
    for key in component_keys:
        if key in cost:
            component_total += _coerce_non_negative_float(cost.get(key))
            matched = True
    if matched and component_total > 0:
        return component_total
    return None


def estimate_public_api_cost_usd(
    *, model_name: str, usage: Mapping[str, Any]
) -> float | None:
    """Estimate cost from usage tokens using public API pricing."""
    normalized_model = normalize_model_name(model_name)
    pricing = MODEL_TOKEN_PRICING_USD_PER_MILLION.get(normalized_model)
    if pricing is None:
        return None

    input_tokens = _int_from_candidates(
        usage,
        ("input_tokens", "prompt_tokens", "input_token_count", "prompt_token_count"),
    )
    output_tokens = _int_from_candidates(
        usage,
        (
            "output_tokens",
            "completion_tokens",
            "output_token_count",
            "completion_token_count",
        ),
    )

    if "cache_read" in pricing:
        cache_read_tokens = _int_from_candidates(
            usage,
            ("cache_read_input_tokens",),
        )
        cache_creation_tokens = _int_from_candidates(
            usage,
            ("cache_creation_input_tokens",),
        )
        cache_creation = _mapping(usage.get("cache_creation"))
        cache_creation_5m = _coerce_non_negative_int(
            cache_creation.get("ephemeral_5m_input_tokens")
        )
        cache_creation_1h = _coerce_non_negative_int(
            cache_creation.get("ephemeral_1h_input_tokens")
        )
        accounted_creation = cache_creation_5m + cache_creation_1h
        remaining_creation = max(0, cache_creation_tokens - accounted_creation)
        # When the provider reports only aggregate cache-creation tokens, assume
        # the default short-lived write tier.
        cache_creation_5m += remaining_creation
        if (
            input_tokens <= 0
            and output_tokens <= 0
            and cache_read_tokens <= 0
            and cache_creation_tokens <= 0
        ):
            return None
        return (
            (input_tokens * pricing["input"])
            + (cache_read_tokens * pricing["cache_read"])
            + (cache_creation_5m * pricing["cache_write_5m"])
            + (cache_creation_1h * pricing["cache_write_1h"])
            + (output_tokens * pricing["output"])
        ) / 1_000_000

    cached_input_tokens = _int_from_candidates(
        usage,
        ("cached_input_tokens", "input_cached_tokens", "cached_prompt_tokens"),
    )
    details = _mapping(
        usage.get("input_tokens_details") or usage.get("prompt_tokens_details")
    )
    cached_input_tokens = max(
        cached_input_tokens,
        _coerce_non_negative_int(details.get("cached_tokens")),
    )
    uncached_input_tokens = max(0, input_tokens - cached_input_tokens)
    if input_tokens <= 0 and output_tokens <= 0:
        return None
    return (
        (uncached_input_tokens * pricing["input"])
        + (cached_input_tokens * pricing["cached_input"])
        + (output_tokens * pricing["output"])
    ) / 1_000_000
