"""HandyDandy: sessions that hand off to themselves.

Watches session size after every turn. Past the limit it tells you (advise), has the
agent write a handoff (handoff), or writes the handoff and starts a fresh session from
it (relay). See README.md.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from .core import Engine, Settings

logger = logging.getLogger(__name__)
PLUGIN_ID = "handydandy"


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home  # type: ignore
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _data_dir() -> Path:
    try:
        from plugins.plugin_storage import plugin_data_dir  # type: ignore
        return Path(plugin_data_dir(PLUGIN_ID))
    except Exception:
        return _hermes_home() / "plugin-data" / PLUGIN_ID


def _ctx_limit(model: str, provider: str, base_url: str):
    # Read-only lookup of the model's context window. Optional: on failure the
    # percentage check is skipped and the token/message limits still apply.
    from agent.model_metadata import get_model_context_length  # type: ignore
    return get_model_context_length(model, base_url=base_url or "", provider=provider or "")


def register(ctx):
    def settings() -> Settings:
        return Settings.from_getter(lambda k, d: ctx.get_config(k, d))

    def profile() -> str:
        try:
            return ctx.profile_name or "default"
        except Exception:
            return "default"

    engine = Engine(settings, _data_dir(), _hermes_home(), profile, ctx_limit_fn=_ctx_limit)

    def pre_api_request(**kw):
        try:
            engine.on_pre_api_request(**kw)
        except Exception:
            logger.debug("handydandy pre_api_request failed", exc_info=True)

    def transform_llm_output(**kw):
        try:
            return engine.on_transform_llm_output(**kw)
        except Exception:
            logger.debug("handydandy transform_llm_output failed", exc_info=True)
            return None

    def pre_llm_call(**kw):
        try:
            return engine.on_pre_llm_call(**kw)
        except Exception:
            logger.debug("handydandy pre_llm_call failed", exc_info=True)
            return None

    ctx.register_hook("pre_api_request", pre_api_request)
    ctx.register_hook("transform_llm_output", transform_llm_output)
    ctx.register_hook("pre_llm_call", pre_llm_call)
