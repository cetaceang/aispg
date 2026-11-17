"""Own WARP lifecycle and prove egress; never silently fall back to direct traffic."""

from __future__ import annotations

import asyncio
import configparser
import contextlib
import io
import ipaddress
import os
import socket
import subprocess
from pathlib import Path

import httpx

from aistudio_api.config import Settings, settings
from aistudio_api.infrastructure.utils.logger import get_logger
from aistudio_api.infrastructure.warp.binaries import find_or_install_binary

log = get_logger("warp")
TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"


def parse_trace(text: str) -> dict[str, str]:
    trace = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    if trace.get("warp") not in {"on", "plus"}:
        raise RuntimeError(
            "WARP egress check failed: trace does not report warp=on/plus"
        )
    ipaddress.ip_address(trace.get("ip", ""))
    return {key: trace[key] for key in ("warp", "ip", "loc") if key in trace}


def render_config(profile: str, port: int, endpoint: str = "") -> str:
    """Convert a wgcf/wg-quick profile to a loopback-only userspace proxy."""
    if not 1 <= port <= 65535:
        raise ValueError("WARP SOCKS port must be between 1 and 65535")
    source = configparser.ConfigParser(interpolation=None)
    source.read_string(profile)
    config = configparser.ConfigParser(interpolation=None)
    config.optionxform = lambda optionstr: optionstr
    for section, names in {
        "Interface": ("Address", "PrivateKey", "MTU", "DNS"),
        "Peer": (
            "PublicKey",
            "PresharedKey",
            "Endpoint",
            "AllowedIPs",
            "PersistentKeepalive",
        ),
    }.items():
        config[section] = {
            name: source.get(section, name)
            for name in names
            if source.has_option(section, name)
        }
    for section, name in (
        ("Interface", "Address"),
        ("Interface", "PrivateKey"),
        ("Peer", "PublicKey"),
        ("Peer", "Endpoint"),
    ):
        if not config.has_option(section, name):
            raise ValueError(f"WARP profile missing [{section}] {name}")
    config["Interface"].setdefault("DNS", "1.1.1.1")
    config["Peer"].setdefault("PersistentKeepalive", "25")
    if endpoint:
        if any(char in endpoint for char in "\r\n"):
            raise ValueError("Invalid WARP endpoint")
        config["Peer"]["Endpoint"] = endpoint
    config["Socks5"] = {"BindAddress": f"127.0.0.1:{port}"}
    config["Resolve"] = {"ResolveStrategy": "ipv4"}
    out = io.StringIO()
    config.write(out)
    return out.getvalue()


class WarpManager:
    """One application-owned wireproxy; registration and keys survive restarts."""

    def __init__(self, config: Settings = settings) -> None:
        self.config = config
        if not config.warp_dir.strip():
            raise ValueError("WARP state directory must not be empty")
        expected = f"socks5://127.0.0.1:{config.warp_port}"
        self.enabled = config.warp_enabled and config.proxy_url == expected
        self.directory = Path(config.warp_dir).expanduser().resolve()
        self.process: asyncio.subprocess.Process | None = None
        self._log_file: io.BufferedWriter | None = None
        self.trace: dict[str, str] = {}

    def _prepare(self) -> tuple[Path, Path]:
        self.directory.mkdir(parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        binary = find_or_install_binary("wireproxy", self.directory / "bin")
        profile = (
            Path(self.config.warp_profile).expanduser()
            if self.config.warp_profile
            else self.directory / "wgcf-profile.conf"
        )
        if not profile.is_file():
            if self.config.warp_profile:
                raise FileNotFoundError(f"WARP profile not found: {profile}")
            wgcf = find_or_install_binary("wgcf", self.directory / "bin")
            # Don't leak proxy environment into bootstrap registration.
            env = {
                k: v
                for k, v in os.environ.items()
                if k.lower() not in {"http_proxy", "https_proxy", "all_proxy"}
            }
            commands = []
            if not (self.directory / "wgcf-account.toml").is_file():
                commands.append([str(wgcf), "register", "--accept-tos"])
            commands.append([str(wgcf), "generate"])
            with (self.directory / "registration.log").open("ab") as output:
                for cmd in commands:
                    result = subprocess.run(
                        cmd,
                        cwd=self.directory,
                        env=env,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        timeout=90,
                        check=False,
                    )
                    if result.returncode:
                        raise RuntimeError(
                            "WARP registration failed; see warp/registration.log or supply AISTUDIO_WARP_PROFILE"
                        )
        config_path = self.directory / "wireproxy.conf"
        config_path.write_text(
            render_config(
                profile.read_text(), self.config.warp_port, self.config.warp_endpoint
            )
        )
        for name in (
            "wgcf-account.toml",
            "wgcf-profile.conf",
            "wireproxy.conf",
            "registration.log",
            "wireproxy.log",
        ):
            path = self.directory / name
            if path.is_file():
                path.chmod(0o600)
        return binary, config_path

    async def verify(self) -> dict[str, str]:
        async with httpx.AsyncClient(
            proxy=self.config.proxy_url, trust_env=False, timeout=6.0
        ) as client:
            response = await client.get(TRACE_URL)
            response.raise_for_status()
        self.trace = parse_trace(response.text)
        return self.trace

    async def start(self) -> None:
        if not self.enabled:
            return
        if self.process is not None:
            raise RuntimeError("WARP manager already started")
        # Refuse to reuse an arbitrary listener on our configured port.
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", self.config.warp_port)) == 0:
                raise RuntimeError(f"WARP port {self.config.warp_port} already in use")
        try:
            binary, config_path = await asyncio.to_thread(self._prepare)
            self._log_file = (self.directory / "wireproxy.log").open("ab")
            (self.directory / "wireproxy.log").chmod(0o600)
            self.process = await asyncio.create_subprocess_exec(
                str(binary),
                "-c",
                str(config_path),
                stdout=self._log_file,
                stderr=subprocess.STDOUT,
            )
            deadline = (
                asyncio.get_running_loop().time() + self.config.warp_start_timeout
            )
            last_error: Exception | None = None
            while asyncio.get_running_loop().time() < deadline:
                if self.process.returncode is not None:
                    raise RuntimeError("wireproxy exited; see warp/wireproxy.log")
                try:
                    await self.verify()
                    log.info(
                        "WARP 出口已验证: warp=%s, ip=%s, loc=%s",
                        self.trace.get("warp"),
                        self.trace.get("ip"),
                        self.trace.get("loc"),
                    )
                    return
                except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                    last_error = exc
                    await asyncio.sleep(0.5)
            raise RuntimeError(
                f"WARP tunnel not ready; refusing direct fallback ({type(last_error).__name__}). Check UDP connectivity / AISTUDIO_WARP_ENDPOINT"
            )
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
        process, self.process = self.process, None
        if process is not None:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=3)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            else:
                await process.wait()
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None
        self.trace = {}


warp_manager = WarpManager()
