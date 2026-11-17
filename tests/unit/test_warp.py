"""WARP policy, provisioning, fail-closed lifecycle, and signer egress contracts."""

from __future__ import annotations

import asyncio
import configparser
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from aistudio_api.config import discover_proxy_url, settings
from aistudio_api.infrastructure.warp import binaries
from aistudio_api.infrastructure.warp.manager import (
    WarpManager,
    parse_trace,
    render_config,
)

PROFILE = """[Interface]
PrivateKey = AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=
Address = 172.16.0.2/32, 2606:4700:110:1234::2/128
MTU = 1280
DNS = 1.1.1.1
PostUp = must-not-execute
[Peer]
PublicKey = AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = engage.cloudflareclient.com:2408
"""


@pytest.fixture
def warp_config(tmp_path):
    return replace(
        settings,
        warp_enabled=True,
        warp_port=18108,
        proxy_url="socks5://127.0.0.1:18108",
        warp_dir=str(tmp_path / "warp"),
        warp_profile="",
        warp_endpoint="",
        warp_start_timeout=0.01,
    )


def test_default_egress_is_warp_not_ambient_proxy(monkeypatch):
    monkeypatch.delenv("AISTUDIO_PROXY", raising=False)
    monkeypatch.delenv("AISTUDIO_WARP_ENABLED", raising=False)
    monkeypatch.delenv("AISTUDIO_WARP_PORT", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://ambient.invalid:7890")
    assert discover_proxy_url() == "socks5://127.0.0.1:1080"


def test_explicit_proxy_and_disable_are_deliberate_overrides(monkeypatch):
    monkeypatch.setenv("AISTUDIO_PROXY", "http://explicit.invalid:8000")
    assert discover_proxy_url() == "http://explicit.invalid:8000"
    monkeypatch.delenv("AISTUDIO_PROXY")
    monkeypatch.setenv("AISTUDIO_WARP_ENABLED", "0")
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(name, raising=False)
    assert discover_proxy_url() is None


@pytest.mark.parametrize("port", ["0", "65536", "invalid"])
def test_invalid_port_cannot_silently_choose_direct(monkeypatch, port):
    monkeypatch.delenv("AISTUDIO_PROXY", raising=False)
    monkeypatch.setenv("AISTUDIO_WARP_ENABLED", "1")
    monkeypatch.setenv("AISTUDIO_WARP_PORT", port)
    with pytest.raises(ValueError):
        discover_proxy_url()


def test_profile_is_loopback_only_and_no_wgquick_commands():
    text = render_config(PROFILE, 1080, "162.159.192.1:2408")
    config = configparser.ConfigParser()
    config.read_string(text)
    assert config["Socks5"]["BindAddress"] == "127.0.0.1:1080"
    assert config["Peer"]["Endpoint"] == "162.159.192.1:2408"
    assert config["Resolve"]["ResolveStrategy"] == "ipv4"
    assert not config.has_option("Interface", "PostUp")


@pytest.mark.parametrize("profile", ["", "[Interface]\nPrivateKey=key\n"])
def test_incomplete_profile_is_rejected(profile):
    with pytest.raises(ValueError, match="missing"):
        render_config(profile, 1080)


def test_prepare_reuses_profile_without_registering(warp_config, monkeypatch):
    manager = WarpManager(warp_config)
    manager.directory.mkdir()
    unrelated = manager.directory / "unrelated-file"
    unrelated.write_text("preserved")
    unrelated.chmod(0o644)
    (manager.directory / "wgcf-profile.conf").write_text(PROFILE)
    find = MagicMock(return_value=Path("/bin/true"))
    monkeypatch.setattr(
        "aistudio_api.infrastructure.warp.manager.find_or_install_binary", find
    )
    binary, config = manager._prepare()
    assert binary == Path("/bin/true")
    find.assert_called_once_with("wireproxy", manager.directory / "bin")
    assert config.stat().st_mode & 0o777 == 0o600
    assert manager.directory.stat().st_mode & 0o777 == 0o700
    assert unrelated.stat().st_mode & 0o777 == 0o644


def test_missing_custom_profile_does_not_register(warp_config, monkeypatch):
    manager = WarpManager(
        replace(
            warp_config, warp_profile=str(Path(warp_config.warp_dir) / "missing.conf")
        )
    )
    monkeypatch.setattr(
        "aistudio_api.infrastructure.warp.manager.find_or_install_binary",
        lambda *_: Path("/bin/true"),
    )
    with pytest.raises(FileNotFoundError):
        manager._prepare()


def test_binary_hash_mismatch_never_installs(tmp_path, monkeypatch):
    monkeypatch.setattr(binaries, "platform_key", lambda: "linux_arm64")
    response = MagicMock(content=b"tampered release")
    client = MagicMock()
    client.return_value.__enter__.return_value.get.return_value = response
    monkeypatch.setattr(binaries.httpx, "Client", client)
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        binaries.install_binary("wireproxy", tmp_path)
    assert not (tmp_path / "wireproxy").exists()
    assert client.call_args.kwargs["trust_env"] is False


@pytest.mark.parametrize("mode", ["off", "", "unknown"])
def test_readiness_requires_real_warp_trace(mode):
    with pytest.raises(RuntimeError, match="warp=on"):
        parse_trace(f"warp={mode}\nip=203.0.113.1\nloc=US\n")


def test_trace_returns_only_egress_fields():
    assert parse_trace("warp=on\nip=104.28.0.1\nloc=US\nsecret=ignored\n") == {
        "warp": "on",
        "ip": "104.28.0.1",
        "loc": "US",
    }


async def test_external_proxy_does_not_start_embedded_process(warp_config):
    manager = WarpManager(
        replace(warp_config, proxy_url="http://external.invalid:8000")
    )
    await manager.start()
    assert manager.process is None
    assert not manager.directory.exists()


async def test_failed_tunnel_is_stopped_without_direct_fallback(
    warp_config, monkeypatch
):
    manager = WarpManager(warp_config)
    manager.directory.mkdir()
    manager._prepare = MagicMock(
        return_value=(Path("/bin/true"), manager.directory / "config")
    )
    process = SimpleNamespace(returncode=None, wait=AsyncMock(), kill=MagicMock())
    process.terminate = MagicMock(side_effect=lambda: setattr(process, "returncode", 0))
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    manager.verify = AsyncMock(side_effect=httpx.ConnectError("tunnel down"))
    with pytest.raises(RuntimeError, match="refusing direct fallback"):
        await manager.start()
    process.terminate.assert_called_once()
    assert manager.process is None
    assert warp_config.proxy_url == "socks5://127.0.0.1:18108"


async def test_occupied_socks_port_is_not_reused(warp_config):
    server = await asyncio.start_server(
        lambda _r, writer: writer.close(), "127.0.0.1", 0
    )
    try:
        port = server.sockets[0].getsockname()[1]
        manager = WarpManager(
            replace(warp_config, warp_port=port, proxy_url=f"socks5://127.0.0.1:{port}")
        )
        with pytest.raises(RuntimeError, match="already in use"):
            await manager.start()
    finally:
        server.close()
        await server.wait_closed()


async def test_lifespan_orders_warp_before_signing_and_cleans_up(monkeypatch):
    from aistudio_api.api import app as application
    from aistudio_api.infrastructure.waa import daemon
    from aistudio_api.infrastructure.warp.manager import warp_manager

    events = []

    async def start():
        events.append("warp")

    async def stop():
        events.append("warp_stop")

    @asynccontextmanager
    async def gateway(_app):
        events.append("gateway")
        try:
            yield
        finally:
            events.append("gateway_stop")

    monkeypatch.setattr(warp_manager, "start", start)
    monkeypatch.setattr(warp_manager, "stop", stop)
    monkeypatch.setattr(daemon, "ensure_signer_ready", lambda: events.append("signer"))
    monkeypatch.setattr(
        daemon, "stop_signer_daemon", lambda: events.append("signer_stop")
    )
    monkeypatch.setattr(application, "_gateway_lifespan", gateway)
    with pytest.raises(ValueError, match="shutdown"):
        async with application.lifespan(application.app):
            raise ValueError("shutdown")
    assert events == [
        "warp",
        "signer",
        "gateway",
        "gateway_stop",
        "signer_stop",
        "warp_stop",
    ]


async def test_failed_warp_blocks_gateway_start(monkeypatch):
    from aistudio_api.api import app as application
    from aistudio_api.infrastructure.waa import daemon
    from aistudio_api.infrastructure.warp.manager import warp_manager

    monkeypatch.setattr(
        warp_manager, "start", AsyncMock(side_effect=RuntimeError("no tunnel"))
    )
    stop = AsyncMock()
    signer = MagicMock()
    monkeypatch.setattr(warp_manager, "stop", stop)
    monkeypatch.setattr(daemon, "ensure_signer_ready", signer)
    monkeypatch.setattr(daemon, "stop_signer_daemon", lambda: None)
    with pytest.raises(RuntimeError, match="no tunnel"):
        async with application.lifespan(application.app):
            pytest.fail("gateway must not start")
    signer.assert_not_called()
    stop.assert_awaited_once()


def test_gateway_no_longer_instantiates_browser():
    from aistudio_api.infrastructure.gateway.client import AIStudioClient
    from aistudio_api.infrastructure.gateway.session import GatewaySession
    from aistudio_api.infrastructure.gateway.transport import HTTPStreamTransport

    client = AIStudioClient()
    assert isinstance(client.session, GatewaySession)
    assert isinstance(client.session._transport, HTTPStreamTransport)
    assert not hasattr(client.session, "page")


@pytest.mark.parametrize(
    ("url", "local"),
    [
        ("http://127.0.0.1:8081", True),
        ("http://localhost:8081", True),
        ("http://[::1]:8081", True),
        ("https://127.0.0.1.example.test:8081", False),
        ("https://example.test/localhost", False),
    ],
)
def test_signer_loopback_detection_uses_hostname(url, local):
    from aistudio_api.infrastructure.waa.signer import is_local_signer

    assert is_local_signer(url) is local


@pytest.mark.parametrize("local", [True, False])
async def test_signer_proxy_contract(local):
    from aistudio_api.infrastructure.waa.signer import RemoteProofSigner

    proxy = "socks5://127.0.0.1:1080"

    def handler(request):
        payload = json.loads(request.content)
        assert payload.get("proxy") == (proxy if local else None)
        return httpx.Response(200, json={"ok": True, "proof": "!test"})

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    signer = RemoteProofSigner(
        server_url="http://127.0.0.1:8081" if local else "https://remote.test",
        proxy_url=proxy,
    )
    try:
        with patch(
            "aistudio_api.infrastructure.waa.signer.httpx.AsyncClient",
            return_value=mock_client,
        ) as factory:
            assert await signer.mint_proof(cookies={"SID": "test"}) == "!test"
        assert factory.call_args.kwargs["proxy"] == (None if local else proxy)
        assert factory.call_args.kwargs["trust_env"] is False
    finally:
        await signer.close()


def test_missing_local_signer_cannot_switch_remote_in_warp_mode(tmp_path, monkeypatch):
    from aistudio_api.infrastructure.waa import daemon

    monkeypatch.setattr(daemon, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        daemon, "get_active_waa_server", lambda: {"url": "http://127.0.0.1:8081"}
    )
    monkeypatch.setattr(daemon, "is_service_healthy", lambda _: False)
    monkeypatch.setattr(daemon, "stop_signer_daemon", lambda: None)
    monkeypatch.setattr(settings, "warp_enabled", True)
    switch = MagicMock()
    monkeypatch.setattr(daemon, "switch_active_waa_server", switch)
    with pytest.raises(RuntimeError, match="禁止静默切换"):
        daemon.ensure_signer_ready()
    switch.assert_not_called()


@pytest.mark.parametrize("process", [None, SimpleNamespace(returncode=1)])
async def test_dead_warp_is_not_healthy(process, monkeypatch):
    from aistudio_api.api.app import app
    from aistudio_api.infrastructure.warp.manager import warp_manager

    monkeypatch.setattr(warp_manager, "enabled", True)
    monkeypatch.setattr(warp_manager, "process", process)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for path in ("/health", "/network/egress"):
            response = await client.get(path)
            assert response.status_code == 503


async def test_egress_probe_requires_console_auth(monkeypatch):
    from aistudio_api.api.app import app
    from aistudio_api.infrastructure.warp.manager import warp_manager

    monkeypatch.setattr(settings, "web_password", "test-console-password")
    monkeypatch.setattr(warp_manager, "enabled", True)
    monkeypatch.setattr(warp_manager, "process", SimpleNamespace(returncode=None))
    verify = AsyncMock(return_value={"warp": "on", "ip": "104.28.0.1", "loc": "US"})
    monkeypatch.setattr(warp_manager, "verify", verify)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/network/egress")
        assert response.status_code == 401
        verify.assert_not_awaited()
        response = await client.get(
            "/network/egress", headers={"Authorization": "Bearer test-console-password"}
        )
        assert response.status_code == 200
        assert response.json()["warp"] == "on"
        verify.assert_awaited_once()


def test_relative_warp_directory_is_resolved(warp_config, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    manager = WarpManager(replace(warp_config, warp_dir="relative-warp"))
    assert manager.directory == tmp_path / "relative-warp"


def test_empty_warp_directory_is_rejected(warp_config):
    with pytest.raises(ValueError, match="must not be empty"):
        WarpManager(replace(warp_config, warp_dir=""))
