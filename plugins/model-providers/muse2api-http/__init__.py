from __future__ import annotations

"""Hermes Agent model-provider plugin for muse2api-http.

Drop this directory (or copy it) into:
  $HERMES_HOME/plugins/model-providers/muse2api-http/

and set the API key so Hermes can route through the muse2api-http OpenAI-compatible
surface:

  hermes config set MUSE2API_KEY=<your-key>
  hermes config hermes config set model.provider muse2api-http

or select the provider in the `/model` picker.
"""

try:
    from providers import register_provider
    from providers.base import ProviderProfile
except Exception as exc:  # pragma: no cover - only reachable outside Hermes
    raise RuntimeError(
        "muse2api-http plugin must be installed under Hermes' providers/ context "
        f"(got: {exc}). Copy this directory to $HERMES_HOME/plugins/model-providers/muse2api-http/"
    ) from exc

profile = ProviderProfile(
    name="muse2api-http",
    aliases=("muse",),
    api_mode="chat_completions",
    display_name="muse2api-http (muse.ai bridge)",
    description="Reverse-engineered muse.ai OpenAI-compatible endpoint serving muse-spark",
    signup_url="https://muse.ai",
    env_vars=("MUSE2API_KEY", "MUSE2API_BASE_URL"),
    base_url="http://127.0.0.1:8130/v1",
    models_url="http://127.0.0.1:8130/v1/models",
    auth_type="api_key",
    supports_streaming=True,
    supports_vision=True,
    fallback_models=(
        "muse-spark",
        "gpt-5",
        "gpt-4o",
        "gpt-4.1",
        "gpt-4o-mini",
        "claude-sonnet-4",
        "claude-3-5-sonnet",
        "deepseek-chat",
    ),
    default_aux_model="muse-spark",
    model_capabilities={
        "muse-spark": {
            "supports_tools": True,
            "supports_vision": True,
        },
    },
)

register_provider(profile)
