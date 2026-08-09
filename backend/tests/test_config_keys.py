"""The key panel: `set_runtime_key`, the `origin` provenance, and `PUT /config/keys`.

The load-bearing tests here are the two about what the endpoint must *not* do — write a
name outside the allowlist, and hand a key value back to a caller. Both are the whole
safety of letting a browser set a server secret (ADR 61).
"""

from __future__ import annotations

import httpx
import pytest

import config
from api.main import app


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def _clean_runtime_keys():
    """`_RUNTIME_SET` is module state, so a test that sets a key must not leak it."""
    before = set(config._RUNTIME_SET)
    yield
    config._RUNTIME_SET.clear()
    config._RUNTIME_SET.update(before)


# ---------- set_runtime_key ----------


def test_set_runtime_key_writes_an_allowlisted_name(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    config.set_runtime_key("TAVILY_API_KEY", "tvly-abc")

    assert config.get_settings().tavily_api_key == "tvly-abc"


def test_set_runtime_key_refuses_anything_else(monkeypatch):
    """Without the allowlist this endpoint repoints the database, which is not a key."""
    with pytest.raises(ValueError):
        config.set_runtime_key("DATABASE_PATH", "/tmp/somewhere-else.db")

    with pytest.raises(ValueError):
        config.set_runtime_key("FRONTEND_DIR", "/tmp/anything")


def test_an_empty_value_clears_the_key(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-abc")
    config.set_runtime_key("TAVILY_API_KEY", "")

    assert config.get_settings().tavily_api_key is None
    assert config.origin("TAVILY_API_KEY") == "unset"


# ---------- origin ----------


def test_origin_says_session_for_a_runtime_key(monkeypatch):
    """A runtime key used to report `.env` — a lie about provenance in the one place a
    reader goes to check provenance."""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    config.set_runtime_key("TAVILY_API_KEY", "tvly-abc")

    assert config.origin("TAVILY_API_KEY") == "session"


def test_origin_says_environment_for_a_preset_variable():
    """`ANTHROPIC_API_KEY` is exported by conftest before `config` snapshots it."""
    assert config.origin("ANTHROPIC_API_KEY") in ("environment", ".env")


def test_origin_says_unset_when_nothing_set_it(monkeypatch):
    monkeypatch.delenv("WIKIPEDIA_API_KEY", raising=False)
    assert config.origin("WIKIPEDIA_API_KEY") == "unset"


# ---------- the endpoint ----------


@pytest.mark.asyncio
async def test_config_reports_where_each_key_came_from(client):
    resp = await client.get("/config")

    assert resp.status_code == 200
    keys = resp.json()["keys"]
    assert set(keys) == {"llm", "llm_var", "tavily", "wikipedia"}
    assert keys["llm_var"] in ("ANTHROPIC_API_KEY", "PYDANTIC_AI_GATEWAY_API_KEY")
    for name in ("llm", "tavily", "wikipedia"):
        assert keys[name] in ("environment", ".env", "session", "unset")


@pytest.mark.asyncio
async def test_the_llm_row_follows_the_key_that_credentials_the_model(
    client, monkeypatch
):
    """A gateway install has no `ANTHROPIC_API_KEY`, so a panel hard-wired to that name
    reported "unset" on a working server and wrote a key the gateway then overruled."""
    monkeypatch.setenv("PYDANTIC_AI_GATEWAY_API_KEY", "pylf_v1_test")

    resp = await client.put("/config/keys", json={"llm_api_key": "pylf_v1_new"})

    assert resp.json()["keys"]["llm_var"] == "PYDANTIC_AI_GATEWAY_API_KEY"
    assert config.get_settings().pydantic_ai_gateway_api_key == "pylf_v1_new"
    assert config.get_settings().anthropic_api_key != "pylf_v1_new"


@pytest.mark.asyncio
async def test_the_llm_row_falls_back_to_anthropic(client, monkeypatch):
    monkeypatch.delenv("PYDANTIC_AI_GATEWAY_API_KEY", raising=False)

    resp = await client.put("/config/keys", json={"llm_api_key": "sk-ant-new"})

    assert resp.json()["keys"]["llm_var"] == "ANTHROPIC_API_KEY"
    assert config.get_settings().anthropic_api_key == "sk-ant-new"


@pytest.mark.asyncio
async def test_setting_a_key_takes_effect_on_the_next_read(client, monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    resp = await client.put("/config/keys", json={"tavily_api_key": "tvly-abc"})

    assert resp.status_code == 200
    assert resp.json()["keys"]["tavily"] == "session"
    assert resp.json()["search_enabled"] is True
    assert config.get_settings().tavily_api_key == "tvly-abc"


@pytest.mark.asyncio
async def test_an_omitted_field_leaves_that_key_alone(client, monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-keep-me")

    await client.put("/config/keys", json={"wikipedia_api_key": "wiki-abc"})

    assert config.get_settings().tavily_api_key == "tvly-keep-me"


@pytest.mark.asyncio
async def test_no_response_ever_contains_a_key_value(client, monkeypatch):
    """Write-only is the point. A route that echoes the value puts a secret in every
    proxy log and browser cache between here and the user."""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    secret = "tvly-do-not-echo-this"

    put = await client.put("/config/keys", json={"tavily_api_key": secret})
    get = await client.get("/config")

    assert secret not in put.text
    assert secret not in get.text


@pytest.mark.asyncio
async def test_the_endpoint_needs_the_admin_token_when_not_local(client, monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "the-real-key")

    denied = await client.put("/config/keys", json={"tavily_api_key": "tvly-abc"})
    assert denied.status_code == 403

    allowed = await client.put(
        "/config/keys",
        json={"tavily_api_key": "tvly-abc"},
        headers={"Authorization": "Bearer the-real-key"},
    )
    assert allowed.status_code == 200
