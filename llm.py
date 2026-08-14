"""Claude API ラッパ。モデル ID はここに一元化する（rules.md）。"""

import anthropic

MODEL_VERBALIZE = "claude-haiku-4-5"
MODEL_EXTRACT = "claude-haiku-4-5"
MODEL_THREAD = "claude-sonnet-5"
MODEL_RULE_TEXT = "claude-haiku-4-5"
MODEL_EXPLORE = "claude-opus-5"

PROMPT_VERSION = "v1"

client = anthropic.Anthropic()


def usage_dict(response) -> dict:
    u = response.usage
    return {
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cache_read_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_write_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
    }


def cost_usd(usage: dict, model: str) -> float:
    rates = {
        "claude-haiku-4-5": (1.0, 5.0),
        "claude-sonnet-5": (2.0, 10.0),
        "claude-opus-5": (5.0, 25.0),
    }
    in_rate, out_rate = rates.get(model, (0.0, 0.0))
    cache_read_cost = usage.get("cache_read_tokens", 0) / 1_000_000 * in_rate * 0.1
    cache_write_cost = usage.get("cache_write_tokens", 0) / 1_000_000 * in_rate * 1.25
    input_cost = usage.get("input_tokens", 0) / 1_000_000 * in_rate
    output_cost = usage.get("output_tokens", 0) / 1_000_000 * out_rate
    return input_cost + output_cost + cache_read_cost + cache_write_cost
