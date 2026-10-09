from dataclasses import replace
from typing import Any, cast

from agno.models.openai.like import OpenAILike

from ..core.config import agnes_keys, settings
from .streaming import StreamingOpenAILike


def build_primary_model(max_completion_tokens: int | None = None, *, api_key: str | None = None):
    _ = max_completion_tokens  # Compatibility only; provider output is intentionally unlimited.
    return OpenAILike(
        id=settings.AGNES_MODEL,
        api_key=settings.AGNES_API_KEY if api_key is None else api_key,
        base_url=settings.AGNES_BASE_URL,
        provider="Agnes",
    )


model = build_primary_model()
proofreader_model = build_primary_model()


def build_fallback_model(max_completion_tokens: int | None = None):
    _ = max_completion_tokens  # Compatibility only; provider output is intentionally unlimited.
    return StreamingOpenAILike(
        id=settings.CR_PROXY_MODEL,
        api_key=settings.CR_PROXY_API_KEY,
        base_url=settings.CR_PROXY_BASE_URL,
    )


fallback_model = build_fallback_model()
proofreader_fallback_model = build_fallback_model()


def build_run_model(provider: str, model_id: str, *, max_output_tokens: int | None = None):
    """Use the frozen provider/model identity for every stage of one run."""
    _ = max_output_tokens  # Compatibility only; retained budgets are estimates and history identity.
    if provider == "agnes":
        keys = agnes_keys(settings)
        key = keys[0] if keys else ""
        model = build_primary_model()
        if isinstance(model, OpenAILike):
            model.api_key = key
            cast(Any, model)._epubox_keys = keys
    elif provider == "cr_proxy":
        key = settings.CR_PROXY_API_KEY
        model = build_fallback_model()
    else:
        raise ValueError(f"unsupported model provider: {provider}")
    if not key or key in {"sk-", "your-api-key-here"}:
        raise ValueError(f"{provider} API key is not configured")
    if model.id != model_id:
        raise ValueError("configured provider model differs from the frozen run identity")
    return model


def build_key_models(model: Any) -> tuple[Any, ...]:
    """Build one isolated Agnes model per in-memory key marker."""
    keys = getattr(model, "_epubox_keys", None)
    if not keys:
        return (model,)
    return tuple(replace(model, api_key=key, client=None, async_client=None, http_client=None) for key in keys)
