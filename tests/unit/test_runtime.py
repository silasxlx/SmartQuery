import asyncio

import pytest

from excel_agent.api import _cors_origins
from excel_agent.config import AppConfig, RuntimeConfig
from excel_agent.runtime import AppContainer


def test_container_creates_two_thread_executor_and_closes(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(temp_root=str(tmp_path / "runtime")))
    container = AppContainer.create(config)
    assert container.executor._max_workers == 2
    managed_path = container.temp_resources.create_path(".xlsx")
    managed_path.write_bytes(b"temporary")
    asyncio.run(container.close())
    assert container.shutting_down is True
    assert container.executor._shutdown is True
    assert not managed_path.exists()


def test_cors_wildcard_is_rejected(monkeypatch):
    from excel_agent import api

    config = api.get_config()
    original = config.server.allowed_origins
    monkeypatch.setattr(config.server, "allowed_origins", ["*"])
    with pytest.raises(RuntimeError, match="server.allowed_origins"):
        _cors_origins()
    monkeypatch.setattr(config.server, "allowed_origins", original)
