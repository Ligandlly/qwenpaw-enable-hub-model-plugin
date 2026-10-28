# -*- coding: utf-8 -*-
"""Enable the Hub-provided default model inside a Hub-managed runtime.

The plugin is a startup hook.  When it is installed, and on every later
runtime start, it reads the organization model directory the Hub grants to
this runtime, enables the Hub's default model in the personal model pool and
points the runtime default chat model at it.

A runtime that is not managed by a Hub (no model-only capability injected by
the provisioner) is left strictly untouched.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Optional, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from qwenpaw.plugins import PluginContext

LOGGER = logging.getLogger(__name__)

PLUGIN_ID = "enable-hub-model"

# ``qwenpaw.providers.hub_managed.PROVIDER_ID``.  Kept local so importing this
# module never touches the host, which keeps the startup hook cheap and the
# module testable on its own.
HUB_PROVIDER_ID = "hub-managed"

# Model-only directory served by the Hub.  ``QWENPAW_HUB_MODEL_URL`` points at
# the Hub's model listener; ``QWENPAW_HUB_MODEL_TOKEN`` is the per-runtime
# capability issued by ``ModelCatalog.issue_token``.
CATALOG_PATH = "/api/hub/model-runtime/catalog"

# Personal selector state written by ``ProviderManager`` (see
# ``qwenpaw.providers.hub_model_selection``).
SELECTION_FILE = "hub-model-selection.json"

DEFAULT_MODEL_TYPE = "chat"
REQUEST_TIMEOUT = 10.0

# Attempt schedule for the catalog read.  The Hub listener may still be
# binding when a runtime boots, so transient failures are retried in place.
RETRY_DELAYS = (0.0, 1.0, 3.0)

# Hub endpoints live on private container networks.  Never let HTTP(S)_PROXY
# from the runtime image intercept the model-only directory call.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def hub_endpoint(
    environ: Optional[Mapping[str, str]] = None,
) -> Optional[tuple[str, str]]:
    """Return ``(base_url, token)`` for Hub-managed runtimes.

    The Hub docker provisioner injects both variables into every managed
    container (``src/qwenpaw/hub/docker_provisioner.py``).  Without them the
    runtime keeps its own model configuration.
    """
    env: Mapping[str, str] = os.environ if environ is None else environ
    base_url = (env.get("QWENPAW_HUB_MODEL_URL") or "").strip()
    token = (env.get("QWENPAW_HUB_MODEL_TOKEN") or "").strip()
    if not base_url or not token:
        return None
    return base_url.rstrip("/"), token


def fetch_catalog(
    base_url: str,
    token: str,
    *,
    timeout: float = REQUEST_TIMEOUT,
) -> dict[str, Any]:
    """Read the model directory granted to this runtime."""
    request = urllib.request.Request(
        f"{base_url}{CATALOG_PATH}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        method="GET",
    )
    with _OPENER.open(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Hub model catalog is not a JSON object")
    return payload


def catalog_models(catalog: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the well-formed model entries of a catalog response."""
    models = catalog.get("models")
    if not isinstance(models, list):
        return []
    return [
        model
        for model in models
        if isinstance(model, dict)
        and isinstance(model.get("id"), str)
        and model["id"]
    ]


def resolve_hub_default(catalog: Mapping[str, Any]) -> Optional[str]:
    """Return the Hub default model, falling back to the first granted one.

    ``member_catalog`` reports the organization default even when it is not
    granted to this member, so membership is checked before trusting it.
    """
    models = catalog_models(catalog)
    if not models:
        return None
    available = [model["id"] for model in models]
    preferred = catalog.get("default_model_id")
    if isinstance(preferred, str) and preferred in available:
        return preferred
    return available[0]


def select_target(
    catalog: Mapping[str, Any],
    current: Any,
    *,
    force: bool = False,
) -> tuple[Optional[str], str]:
    """Choose which model the default chat slot should use.

    Returns ``(model_id, outcome)`` where outcome is ``applied`` (the Hub
    default wins) or ``kept`` (an existing Hub model selection is preserved).
    """
    hub_default = resolve_hub_default(catalog)
    if hub_default is None:
        return None, "no-hub-models"
    available = {model["id"] for model in catalog_models(catalog)}
    current_provider = getattr(current, "provider_id", None)
    current_model = getattr(current, "model", None)
    if (
        not force
        and current_provider == HUB_PROVIDER_ID
        and current_model in available
    ):
        return current_model, "kept"
    return hub_default, "applied"


def provider_manager() -> Any:
    """Return the host singleton that owns provider/selection state."""
    from qwenpaw.providers.provider_manager import ProviderManager

    return ProviderManager.get_instance()


def record_selection(manager: Any, model_id: str) -> dict[str, Any]:
    """Enable the model in the personal pool.

    The host helper merges the change atomically with concurrent console
    writes.  ``selected`` exposes the model as a configured one, ``seen``
    marks it as read and ``hidden=False`` restores a previously hidden card.
    """
    from qwenpaw.providers import hub_model_selection

    path = Path(manager.root_path) / SELECTION_FILE
    return hub_model_selection.update_selection(
        path,
        model_id,
        selected=True,
        hidden=False,
        seen=True,
    )


def default_model_slot(model_id: str) -> Any:
    """Build the slot the host persists to ``active_model.json``."""
    from qwenpaw.config.config import ModelSlotConfig

    return ModelSlotConfig(provider_id=HUB_PROVIDER_ID, model=model_id)


async def make_default_model(manager: Any, model_id: str) -> None:
    """Point the runtime default chat slot at the Hub model.

    QwenPaw 2.3 exposes typed defaults; 2.2 only has ``activate_model`` for
    the chat slot, which remains the compatible path.
    """
    setter = getattr(manager, "set_default_model", None)
    if callable(setter):
        await setter(DEFAULT_MODEL_TYPE, default_model_slot(model_id))
        return
    await manager.activate_model(HUB_PROVIDER_ID, model_id)


async def _fetch_catalog_with_retries(
    base_url: str,
    token: str,
) -> dict[str, Any]:
    attempts = len(RETRY_DELAYS)
    last_error: Optional[Exception] = None
    for index, delay in enumerate(RETRY_DELAYS, start=1):
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            return await asyncio.to_thread(fetch_catalog, base_url, token)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            last_error = exc
            LOGGER.warning(
                "[%s] Hub model catalog unavailable (attempt %d/%d): %s",
                PLUGIN_ID,
                index,
                attempts,
                exc,
            )
    if last_error is None:  # pragma: no cover - RETRY_DELAYS is non-empty
        raise RuntimeError("Hub model catalog retry schedule is empty")
    raise last_error


async def apply_hub_default_model(
    *,
    force: bool = False,
    manager: Any = None,
    environ: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """Enable the Hub default model and adopt it as the runtime default.

    Args:
        force: Reset an existing, still-valid Hub model selection back to the
            organization default.  Without it a user's own Hub choice wins.
        manager: Injectable provider manager (tests); the host singleton is
            resolved lazily otherwise.
        environ: Injectable environment (tests); ``os.environ`` otherwise.

    Returns:
        A machine-readable summary: ``status`` is ``skipped``, ``applied`` or
        ``kept``.

    Raises:
        Exception: when the Hub catalog cannot be read after all retries or
            the host rejects the model.  The startup hook logs and swallows.
    """
    endpoint = hub_endpoint(environ)
    if endpoint is None:
        return {"status": "skipped", "reason": "not-a-hub-managed-runtime"}

    catalog = await _fetch_catalog_with_retries(*endpoint)
    hub_default = resolve_hub_default(catalog)
    if hub_default is None:
        return {"status": "skipped", "reason": "no-hub-models"}

    if manager is None:
        manager = provider_manager()

    model_id, outcome = select_target(
        catalog,
        manager.active_model,
        force=force,
    )
    if model_id is None:  # pragma: no cover - guarded above
        return {"status": "skipped", "reason": "no-hub-models"}

    await asyncio.to_thread(record_selection, manager, model_id)
    if outcome == "applied":
        await make_default_model(manager, model_id)

    return {
        "status": outcome,
        "provider_id": HUB_PROVIDER_ID,
        "model_id": model_id,
        "hub_default_id": hub_default,
    }


def read_force(config: Any) -> bool:
    """Read the ``force`` option from SDK or legacy plugin config."""
    getter = getattr(config, "get", None)
    if not callable(getter):
        return False
    try:
        return bool(getter("force", False))
    except Exception:  # pylint: disable=broad-exception-caught
        return False


class Plugin:
    """Startup hook that adopts the Hub model directory."""

    def register(self, context: Any) -> None:
        """Register activation on the public SDK or the legacy PluginApi."""
        activate = getattr(context, "on_activate", None)
        if callable(activate):
            activate(self._activate)
            return

        # QwenPaw <= 2.2.x exposes the legacy PluginApi only, whose startup
        # hooks take no arguments; the config snapshot is captured here.
        register_hook = getattr(context, "register_startup_hook", None)
        if not callable(register_hook):
            raise RuntimeError(
                "enable-hub-model requires on_activate() or "
                "register_startup_hook()",
            )
        config = getattr(context, "config", None)

        async def startup() -> None:
            await self._apply(config)

        register_hook("enable_hub_model", startup, priority=50)

    async def _activate(self, context: "PluginContext") -> None:
        await self._apply(getattr(context, "config", None))

    async def _apply(self, config: Any) -> None:
        try:
            result = await apply_hub_default_model(force=read_force(config))
        except Exception:  # pylint: disable=broad-exception-caught
            # A transient Hub outage must not mark the plugin (or a fresh
            # install) as failed; the next runtime start retries.
            LOGGER.exception(
                "[%s] could not enable the Hub default model",
                PLUGIN_ID,
            )
            return
        if result["status"] == "skipped":
            LOGGER.info(
                "[%s] skipped: %s",
                PLUGIN_ID,
                result.get("reason"),
            )
            return
        LOGGER.info(
            "[%s] %s model %s (Hub default %s)",
            PLUGIN_ID,
            result["status"],
            result["model_id"],
            result["hub_default_id"],
        )


plugin = Plugin()
