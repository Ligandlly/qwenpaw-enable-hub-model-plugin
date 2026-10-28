# -*- coding: utf-8 -*-
"""Unit tests for the Hub default-model startup hook."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import threading
import types
from collections import namedtuple
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "enable_hub_model.py"

HUB_ENV = {
    "QWENPAW_HUB_MODEL_URL": "http://172.18.0.1:49152",
    "QWENPAW_HUB_MODEL_TOKEN": "runtime-token",
}

Slot = namedtuple("Slot", "provider_id model")


def load_plugin_module():
    spec = importlib.util.spec_from_file_location(
        "enable_hub_model_under_test",
        MODULE_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def plugin():
    return load_plugin_module()


@pytest.fixture()
def slot_stub(plugin, monkeypatch):
    monkeypatch.setattr(
        plugin,
        "default_model_slot",
        lambda model_id: Slot(plugin.HUB_PROVIDER_ID, model_id),
    )


@pytest.fixture()
def selection_calls(plugin, monkeypatch):
    calls: list[tuple[object, str]] = []

    def record_selection(manager, model_id):
        calls.append((manager, model_id))
        return {"selected": [model_id]}

    monkeypatch.setattr(plugin, "record_selection", record_selection)
    return calls


class FakeManager:
    def __init__(self, active=None, root_path=None):
        self.active_model = active
        self.root_path = root_path
        self.default_calls: list[tuple[str, object]] = []

    async def set_default_model(self, model_type, slot):
        self.default_calls.append((model_type, slot))
        self.active_model = slot


class LegacyManager:
    """QwenPaw 2.2 provider manager: no typed defaults, only activate_model."""

    def __init__(self, active=None, root_path=None):
        self.active_model = active
        self.root_path = root_path
        self.activate_calls: list[tuple[str, str]] = []

    async def activate_model(self, provider_id, model_id):
        self.activate_calls.append((provider_id, model_id))
        self.active_model = Slot(provider_id, model_id)


def slot(provider_id, model):
    return types.SimpleNamespace(provider_id=provider_id, model=model)


def catalog(default_model_id=None, models=("hub-a", "hub-b")):
    return {
        "revision": 7,
        "default_model_id": default_model_id,
        "models": [
            {
                "id": model_id,
                "name": model_id.upper(),
                "supports_image": False,
                "input_token_limit": 32768,
                "output_token_limit": 8192,
                "supports_agent_thinking": False,
            }
            for model_id in models
        ],
    }


@pytest.fixture()
def stub_catalog(plugin, monkeypatch):
    """Serve a catalog without touching the network."""

    def install(payload, *, calls=None):
        def fake_fetch(base_url, token, *, timeout=10.0):
            if calls is not None:
                calls.append((base_url, token, timeout))
            return payload

        monkeypatch.setattr(plugin, "fetch_catalog", fake_fetch)

    return install


# ---------------------------------------------------------------------------
# Hub detection and target resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {"QWENPAW_HUB_MODEL_URL": HUB_ENV["QWENPAW_HUB_MODEL_URL"]},
        {"QWENPAW_HUB_MODEL_TOKEN": HUB_ENV["QWENPAW_HUB_MODEL_TOKEN"]},
        {"QWENPAW_HUB_MODEL_URL": "  ", "QWENPAW_HUB_MODEL_TOKEN": "  "},
    ],
)
def test_non_hub_runtime_is_untouched(plugin, environ, selection_calls):
    manager = FakeManager()
    result = asyncio.run(
        plugin.apply_hub_default_model(manager=manager, environ=environ),
    )
    assert result == {
        "status": "skipped",
        "reason": "not-a-hub-managed-runtime",
    }
    assert manager.default_calls == []
    assert selection_calls == []


def test_hub_endpoint_strips_trailing_slash(plugin):
    assert plugin.hub_endpoint(
        {
            "QWENPAW_HUB_MODEL_URL": "http://hub:9000/",
            "QWENPAW_HUB_MODEL_TOKEN": " tok ",
        },
    ) == ("http://hub:9000", "tok")


def test_resolve_hub_default_prefers_admin_default(plugin):
    payload = catalog(default_model_id="hub-b")
    assert plugin.resolve_hub_default(payload) == "hub-b"


def test_resolve_hub_default_ignores_ungranted_default(plugin):
    payload = catalog(default_model_id="not-granted")
    assert plugin.resolve_hub_default(payload) == "hub-a"


def test_resolve_hub_default_without_default_uses_first(plugin):
    assert plugin.resolve_hub_default(catalog()) == "hub-a"


def test_resolve_hub_default_skips_malformed_entries(plugin):
    payload = {
        "default_model_id": "hub-b",
        "models": [
            {"id": ""},
            {"id": None},
            "hub-c",
            {"id": "hub-b"},
            {"name": "missing-id"},
        ],
    }
    assert plugin.catalog_models(payload) == [{"id": "hub-b"}]
    assert plugin.resolve_hub_default(payload) == "hub-b"


# ---------------------------------------------------------------------------
# Applying the default model
# ---------------------------------------------------------------------------


def test_applies_hub_default_when_no_active_model(
    plugin,
    stub_catalog,
    selection_calls,
    slot_stub,
):
    stub_catalog(catalog(default_model_id="hub-b"))
    manager = FakeManager()

    result = asyncio.run(
        plugin.apply_hub_default_model(manager=manager, environ=HUB_ENV),
    )

    assert result == {
        "status": "applied",
        "provider_id": "hub-managed",
        "model_id": "hub-b",
        "hub_default_id": "hub-b",
    }
    assert selection_calls == [(manager, "hub-b")]
    assert manager.default_calls == [
        ("chat", Slot("hub-managed", "hub-b")),
    ]


def test_keeps_existing_valid_hub_selection(
    plugin,
    stub_catalog,
    selection_calls,
    slot_stub,
):
    stub_catalog(catalog(default_model_id="hub-b"))
    manager = FakeManager(active=slot("hub-managed", "hub-a"))

    result = asyncio.run(
        plugin.apply_hub_default_model(manager=manager, environ=HUB_ENV),
    )

    assert result["status"] == "kept"
    assert result["model_id"] == "hub-a"
    assert result["hub_default_id"] == "hub-b"
    assert manager.default_calls == []
    # Still enabled, otherwise a kept model could be an unselected leftover.
    assert selection_calls == [(manager, "hub-a")]


def test_force_resets_to_hub_default(
    plugin,
    stub_catalog,
    selection_calls,
    slot_stub,
):
    stub_catalog(catalog(default_model_id="hub-b"))
    manager = FakeManager(active=slot("hub-managed", "hub-a"))

    result = asyncio.run(
        plugin.apply_hub_default_model(
            force=True,
            manager=manager,
            environ=HUB_ENV,
        ),
    )

    assert result["status"] == "applied"
    assert result["model_id"] == "hub-b"
    assert manager.default_calls == [
        ("chat", Slot("hub-managed", "hub-b")),
    ]


def test_replaces_non_hub_default(
    plugin,
    stub_catalog,
    selection_calls,
    slot_stub,
):
    stub_catalog(catalog(default_model_id="hub-b"))
    manager = FakeManager(active=slot("kilo", "kilo-auto/free"))

    result = asyncio.run(
        plugin.apply_hub_default_model(manager=manager, environ=HUB_ENV),
    )

    assert result["status"] == "applied"
    assert manager.default_calls == [
        ("chat", Slot("hub-managed", "hub-b")),
    ]


def test_replaces_hub_default_that_is_no_longer_granted(
    plugin,
    stub_catalog,
    selection_calls,
    slot_stub,
):
    stub_catalog(catalog(default_model_id="hub-b"))
    manager = FakeManager(active=slot("hub-managed", "retired-model"))

    result = asyncio.run(
        plugin.apply_hub_default_model(manager=manager, environ=HUB_ENV),
    )

    assert result["status"] == "applied"
    assert result["model_id"] == "hub-b"


def test_uses_first_granted_model_when_default_is_ungranted(
    plugin,
    stub_catalog,
    selection_calls,
    slot_stub,
):
    stub_catalog(catalog(default_model_id="not-granted"))
    manager = FakeManager()

    result = asyncio.run(
        plugin.apply_hub_default_model(manager=manager, environ=HUB_ENV),
    )

    assert result["model_id"] == "hub-a"
    assert result["hub_default_id"] == "hub-a"


def test_skips_when_hub_grants_no_models(
    plugin,
    stub_catalog,
    selection_calls,
    slot_stub,
):
    stub_catalog(catalog(default_model_id=None, models=()))
    manager = FakeManager()

    result = asyncio.run(
        plugin.apply_hub_default_model(manager=manager, environ=HUB_ENV),
    )

    assert result == {"status": "skipped", "reason": "no-hub-models"}
    assert manager.default_calls == []
    assert selection_calls == []


def test_uses_legacy_activate_model_on_old_hosts(
    plugin,
    stub_catalog,
    selection_calls,
    slot_stub,
):
    stub_catalog(catalog(default_model_id="hub-b"))
    manager = LegacyManager()

    result = asyncio.run(
        plugin.apply_hub_default_model(manager=manager, environ=HUB_ENV),
    )

    assert result["status"] == "applied"
    assert manager.activate_calls == [("hub-managed", "hub-b")]


# ---------------------------------------------------------------------------
# Catalog retries
# ---------------------------------------------------------------------------


def test_retries_transient_catalog_failures(
    plugin,
    monkeypatch,
    selection_calls,
    slot_stub,
):
    monkeypatch.setattr(plugin, "RETRY_DELAYS", (0.0, 0.0, 0.0))
    attempts: list[int] = []

    def flaky_fetch(base_url, token, *, timeout=10.0):
        attempts.append(1)
        if len(attempts) < 3:
            raise OSError("listener is still binding")
        return catalog(default_model_id="hub-b")

    monkeypatch.setattr(plugin, "fetch_catalog", flaky_fetch)
    manager = FakeManager()

    result = asyncio.run(
        plugin.apply_hub_default_model(manager=manager, environ=HUB_ENV),
    )

    assert len(attempts) == 3
    assert result["status"] == "applied"


def test_raises_when_catalog_stays_unavailable(
    plugin,
    monkeypatch,
    selection_calls,
    slot_stub,
):
    monkeypatch.setattr(plugin, "RETRY_DELAYS", (0.0, 0.0))
    attempts: list[int] = []

    def broken_fetch(base_url, token, *, timeout=10.0):
        attempts.append(1)
        raise OSError("hub unreachable")

    monkeypatch.setattr(plugin, "fetch_catalog", broken_fetch)
    manager = FakeManager()

    with pytest.raises(OSError, match="hub unreachable"):
        asyncio.run(
            plugin.apply_hub_default_model(manager=manager, environ=HUB_ENV),
        )

    assert len(attempts) == 2
    assert manager.default_calls == []
    assert selection_calls == []


# ---------------------------------------------------------------------------
# Host wiring
# ---------------------------------------------------------------------------


def test_record_selection_merges_into_personal_pool(
    plugin,
    monkeypatch,
    tmp_path,
):
    calls: dict[str, object] = {}

    module_stub = types.ModuleType("qwenpaw.providers.hub_model_selection")

    def update_selection(path, model_id, **kwargs):
        calls["path"] = path
        calls["model_id"] = model_id
        calls.update(kwargs)
        return {"selected": [model_id]}

    module_stub.update_selection = update_selection
    package_stub = types.ModuleType("qwenpaw.providers")
    package_stub.__path__ = []
    package_stub.hub_model_selection = module_stub
    root_stub = types.ModuleType("qwenpaw")
    root_stub.__path__ = []
    root_stub.providers = package_stub
    for name, module in (
        ("qwenpaw", root_stub),
        ("qwenpaw.providers", package_stub),
        ("qwenpaw.providers.hub_model_selection", module_stub),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    manager = types.SimpleNamespace(root_path=tmp_path)
    assert plugin.record_selection(manager, "hub-a") == {"selected": ["hub-a"]}
    assert calls == {
        "path": tmp_path / "hub-model-selection.json",
        "model_id": "hub-a",
        "selected": True,
        "hidden": False,
        "seen": True,
    }


def test_resolves_host_provider_manager(
    plugin,
    monkeypatch,
    stub_catalog,
    selection_calls,
    slot_stub,
):
    stub_catalog(catalog(default_model_id="hub-b"))
    manager = FakeManager()
    lookups: list[int] = []

    class ProviderManagerStub:
        @staticmethod
        def get_instance():
            lookups.append(1)
            return manager

    module_stub = types.ModuleType("qwenpaw.providers.provider_manager")
    module_stub.ProviderManager = ProviderManagerStub
    package_stub = types.ModuleType("qwenpaw.providers")
    package_stub.__path__ = []
    package_stub.provider_manager = module_stub
    root_stub = types.ModuleType("qwenpaw")
    root_stub.__path__ = []
    root_stub.providers = package_stub
    for name, module in (
        ("qwenpaw", root_stub),
        ("qwenpaw.providers", package_stub),
        ("qwenpaw.providers.provider_manager", module_stub),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    result = asyncio.run(plugin.apply_hub_default_model(environ=HUB_ENV))

    assert lookups == [1]
    assert result["status"] == "applied"
    assert manager.default_calls == [
        ("chat", Slot("hub-managed", "hub-b")),
    ]


def test_fetch_catalog_reads_the_model_directory(plugin):
    seen: list[dict[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            seen.append(
                {
                    "path": self.path,
                    "authorization": self.headers.get("Authorization", ""),
                    "accept": self.headers.get("Accept", ""),
                },
            )
            body = json.dumps(
                {"revision": 3, "default_model_id": "hub-a", "models": []},
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # keep pytest output clean
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = plugin.fetch_catalog(
            f"http://127.0.0.1:{server.server_address[1]}",
            "tok",
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert payload["revision"] == 3
    assert seen == [
        {
            "path": "/api/hub/model-runtime/catalog",
            "authorization": "Bearer tok",
            "accept": "application/json",
        },
    ]


# ---------------------------------------------------------------------------
# Plugin registration (SDK and legacy)
# ---------------------------------------------------------------------------


class SdkContext:
    def __init__(self, config):
        self.config = config
        self.callbacks = []

    def on_activate(self, callback):
        self.callbacks.append(callback)


class LegacyContext:
    def __init__(self, config):
        self.config = config
        self.hooks = []

    def register_startup_hook(self, hook_name, callback, priority=100):
        self.hooks.append((hook_name, callback, priority))


def record_apply(plugin, monkeypatch, *, error=None):
    calls: list[bool] = []

    async def fake_apply(*, force=False, manager=None, environ=None):
        calls.append(force)
        if error is not None:
            raise error
        return {
            "status": "applied",
            "provider_id": "hub-managed",
            "model_id": "hub-b",
            "hub_default_id": "hub-b",
        }

    monkeypatch.setattr(plugin, "apply_hub_default_model", fake_apply)
    return calls


def test_register_uses_sdk_activation(plugin, monkeypatch):
    calls = record_apply(plugin, monkeypatch)
    context = SdkContext({"force": True})

    plugin.plugin.register(context)

    assert len(context.callbacks) == 1
    asyncio.run(context.callbacks[0](context))
    assert calls == [True]


def test_register_defaults_force_to_false(plugin, monkeypatch):
    calls = record_apply(plugin, monkeypatch)
    context = SdkContext({})

    plugin.plugin.register(context)
    asyncio.run(context.callbacks[0](context))

    assert calls == [False]


def test_register_uses_legacy_startup_hook(plugin, monkeypatch):
    calls = record_apply(plugin, monkeypatch)
    context = LegacyContext({"force": True})

    plugin.plugin.register(context)

    assert len(context.hooks) == 1
    name, callback, priority = context.hooks[0]
    assert name == "enable_hub_model"
    assert priority == 50
    asyncio.run(callback())
    assert calls == [True]


def test_activation_swallows_apply_failures(plugin, monkeypatch):
    record_apply(plugin, monkeypatch, error=OSError("hub unreachable"))
    context = SdkContext({})

    plugin.plugin.register(context)
    # The startup hook must not fail the plugin (and a fresh install).
    assert asyncio.run(context.callbacks[0](context)) is None


def test_read_force_accepts_missing_config(plugin):
    assert plugin.read_force(None) is False
    assert plugin.read_force({}) is False
    assert plugin.read_force({"force": True}) is True
    assert plugin.read_force({"force": "yes"}) is True


def test_manifest_matches_entrypoint(plugin):
    manifest = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["id"] == plugin.PLUGIN_ID
    assert manifest["entry"]["backend"] == MODULE_PATH.name
    assert manifest["type"] == "hook"
    assert manifest["qwenpaw_version"]["min"] == "2.2.2"
    assert set(manifest["sdk"]["capabilities"]) <= {
        "config",
        "logging",
        "models",
    }
