"""A .env in the working directory must not reconfigure network exposure or
redirect the LLM key (M7)."""
from __future__ import annotations

import inspect

import pytest

from hyperdata_terminal.envfile import PROTECTED_KEYS, load_env

HOSTILE = (
    "HYPERDATA_API_HOST=0.0.0.0\n"
    "HYPERDATA_API_PORT=8420\n"
    "HYPERDATA_UNSAFE_PUBLIC_API=1\n"
    "HYPERDATA_API_KEY=attacker-chosen\n"
    "HYPERDATA_CORS_ORIGINS=https://evil.example\n"
    "LLM_BASE_URL=http://attacker.example/v1\n"
    "HYPERDATA_DATA_DIR=/tmp/elsewhere\n"
    "TELEGRAM_CHAT_ID=42\n"
)


def test_cwd_env_cannot_set_protected_keys(tmp_path):
    (tmp_path / ".env").write_text(HOSTILE)
    environ: dict[str, str] = {"LLM_API_KEY": "sk-real-user-key"}
    ignored = load_env(cwd=tmp_path, environ=environ, data_dir=tmp_path / "data")
    for key in PROTECTED_KEYS & set(environ):
        pytest.fail(f"{key} was set from ./.env")
    assert set(ignored) == {
        "HYPERDATA_API_HOST", "HYPERDATA_API_PORT", "HYPERDATA_UNSAFE_PUBLIC_API", "HYPERDATA_API_KEY",
        "HYPERDATA_CORS_ORIGINS", "LLM_BASE_URL", "HYPERDATA_DATA_DIR",
    }
    assert environ["TELEGRAM_CHAT_ID"] == "42"  # ordinary settings still load
    assert environ["LLM_API_KEY"] == "sk-real-user-key"


def test_data_dir_env_and_the_real_environment_may_set_them(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / ".env").write_text("HYPERDATA_API_HOST=0.0.0.0\nLLM_BASE_URL=https://api.example/v1\n")
    environ = {"LLM_BASE_URL": "http://localhost:11434/v1"}
    assert load_env(cwd=tmp_path, environ=environ, data_dir=data) == []
    assert environ["HYPERDATA_API_HOST"] == "0.0.0.0"
    assert environ["LLM_BASE_URL"] == "http://localhost:11434/v1"  # the environment wins


def test_cli_warns_about_ignored_keys(tmp_path, monkeypatch, capsys):
    from hyperdata_terminal import cli

    (tmp_path / ".env").write_text("HYPERDATA_API_HOST=0.0.0.0\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HYPERDATA_API_HOST", raising=False)
    cli._load_env()
    assert "ignored HYPERDATA_API_HOST from ./.env" in capsys.readouterr().err
    import os

    assert "HYPERDATA_API_HOST" not in os.environ


def test_no_module_loads_dotenv_at_import():
    from hyperdata_terminal.data_layer import alerts
    from hyperdata_terminal.strategies import llm_agent

    for module in (alerts, llm_agent):
        assert "load_dotenv" not in inspect.getsource(module)


@pytest.mark.parametrize("url,local", [
    ("http://127.0.0.1:1234/v1", True),
    ("http://localhost:11434/v1", True),
    ("http://[::1]:8080/v1", True),
    ("https://localhost.attacker.tld/v1", False),
    ("http://api.example.com/v1?x=localhost", False),
    ("https://api.openai.com/v1", False),
])
def test_local_llm_check_parses_the_host(url, local):
    from hyperdata_terminal.strategies.llm_agent import is_local_url

    assert is_local_url(url) is local


@pytest.mark.asyncio
@pytest.mark.parametrize("url,key,should_call", [
    ("http://127.0.0.1:1234/v1", "", True),            # LM Studio, no key: allowed (was refused)
    ("https://localhost.attacker.tld/v1", "", False),  # not local: needs a key (was treated as local)
    ("http://remote.example/v1", "sk-secret", False),  # the key never goes over plain http
    ("https://remote.example/v1", "sk-secret", True),
])
async def test_llm_agent_key_transport(monkeypatch, url, key, should_call):
    from hyperdata_terminal.strategies.llm_agent import LLMAgent

    monkeypatch.setenv("LLM_BASE_URL", url)
    monkeypatch.setenv("LLM_API_KEY", key)
    agent = LLMAgent()
    called = []

    async def fake(hub):
        called.append(True)
        return None

    monkeypatch.setattr(agent, "_async_evaluate", fake)
    await agent.evaluate(object())
    assert bool(called) is should_call
