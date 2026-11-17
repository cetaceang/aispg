"""Install pinned, SHA-256 verified WARP tools; never download on each restart."""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import platform
import shutil
import tarfile
import tempfile
from pathlib import Path

import httpx

WIREPROXY_VERSION = "1.1.3"
WGCF_VERSION = "2.3.1"

# SHA-256 of release assets, not a remotely mutable checksum at install time.
_WIREPROXY_HASHES = {
    "linux_amd64": "e88c1d090740373fc606c1bafd81d9a5eadc642cce5667616e20e9d7a444f51c",
    "linux_arm64": "370e00bd2167960d1ecd1c3c1439715bbaa94a0a110a2040468670c9af6021b6",
    "darwin_amd64": "5d89742a0f381d9508d3ad828a0d300dceee24fa49015427eaff42a23f7a50bd",
    "darwin_arm64": "28d34342c48c309b628d9c06ab4efc05b82fba49821f360605c02139c02547a5",
    "windows_amd64": "bce041ea9fe0f8a3351301dcbe29cdf6a523bb25cf9c62f17ebb5699a8051d0f",
}
_WGCF_HASHES = {
    "linux_amd64": "4fb7a136aa470ece11d17949c7db07b040ec9c604742dc8680bcf1889f85d0c8",
    "linux_arm64": "710ab2d8ac145d81a4cf0c11bdb0e05914f77ae2a45f67b87e718d84b4cb7301",
    "darwin_amd64": "de295a28dd7503cd779c9afa2bf2e59435107bace94bdc0b57b66d8c9d9408ad",
    "darwin_arm64": "45be8d5ea91fd0b938a1775fd3e0b29a33b756b6cc67a0a9f305bcf86ff608c2",
    "windows_amd64": "e222fd9443d7cb3264d825a3e86967797fc9c938507c5d93e3cc21e4aed11dcc",
}


def platform_key() -> str:
    system = platform.system().lower()
    if system == "android":
        system = "linux"
    arch = {"aarch64": "arm64", "x86_64": "amd64"}.get(
        platform.machine().lower(), platform.machine().lower()
    )
    key = f"{system}_{arch}"
    if key not in _WIREPROXY_HASHES:
        raise RuntimeError(f"No bundled WARP tools for {key}; provide tool binaries")
    return key


def install_binary(name: str, directory: Path) -> Path:
    """Fetch an official pinned asset, check digest, extract ONLY the executable."""
    key = platform_key()
    suffix = ".exe" if key.startswith("windows_") else ""
    executable = name + suffix
    target = directory / executable
    if target.is_file() and os.access(target, os.X_OK):
        return target
    if name == "wireproxy":
        asset = f"wireproxy_{key}.tar.gz"
        url = f"https://github.com/windtf/wireproxy/releases/download/v{WIREPROXY_VERSION}/{asset}"
        digest = _WIREPROXY_HASHES[key]
    elif name == "wgcf":
        asset = f"wgcf_{WGCF_VERSION}_{key}{suffix}"
        url = f"https://github.com/ViRb3/wgcf/releases/download/v{WGCF_VERSION}/{asset}"
        digest = _WGCF_HASHES[key]
    else:
        raise ValueError(f"Unknown WARP tool: {name}")
    # Bootstrap downloads are necessarily direct: WARP does not exist yet.
    with httpx.Client(follow_redirects=True, timeout=120.0, trust_env=False) as client:
        response = client.get(url)
        response.raise_for_status()
    payload = response.content
    if hashlib.sha256(payload).hexdigest() != digest:
        raise RuntimeError(f"SHA-256 mismatch for {asset}")
    if name == "wireproxy":
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            member = next(
                (
                    m
                    for m in archive.getmembers()
                    if m.isfile() and Path(m.name).name == executable
                ),
                None,
            )
            if member is None:
                raise RuntimeError(f"Missing {executable} in {asset}")
            stream = archive.extractfile(member)
            if stream is None:
                raise RuntimeError(f"Cannot read {executable} in {asset}")
            payload = stream.read()
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as temporary:
        temporary.write(payload)
        staging = Path(temporary.name)
    try:
        staging.chmod(0o700)
        staging.replace(target)
    finally:
        staging.unlink(missing_ok=True)
    return target


def find_or_install_binary(name: str, directory: Path) -> Path:
    override = os.getenv(f"AISTUDIO_{name.upper()}_BINARY")
    if override:
        path = Path(override).expanduser().resolve()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise RuntimeError(f"Configured {name} binary is not executable: {path}")
        return path
    found = shutil.which(name)
    return Path(found).resolve() if found else install_binary(name, directory)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Install embedded WARP tools (no registration)"
    )
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    for name in ("wireproxy", "wgcf"):
        print(install_binary(name, args.directory))


if __name__ == "__main__":
    main()
