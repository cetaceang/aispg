# aispg — Browserless AI Studio API + WARP

Gemini-compatible gateway based on DeconstructedCube/aistudio-api, upstream commit
`fcdc53ee753a9e160686193ac9841bdf97b7227f`. No Chromium, CloakBrowser, CDP, Playwright or Selenium is launched.

The default path is FastAPI → local Go/SpiderMonkey WAA signer → HTTP/2 → embedded
wireproxy SOCKS5 → Cloudflare WARP → Google. Signing, credential probes, cookie rotation,
and inference share the same WARP egress. Tunnel failure never silently enables direct traffic.

## Start

```bash
cp .env.example .env
docker compose up -d --build
```

The image includes the native signer plus pinned SHA-256-verified wireproxy 1.1.3 and wgcf 2.3.1.
No root, privileged container, NET_ADMIN or TUN device is needed. UDP connectivity to WARP is required.
Open http://localhost:8080 and import Google AI Studio cookies in Accounts.

Device registration (`wgcf register --accept-tos`) happens only on first start. Persist `data/warp/`
along with account data. Bootstrap downloads/registration use the existing network; business requests
start only after WARP egress verification. For native installation (Python 3.11+, uv, Go 1.27+, zig/clang/gcc):

```bash
bash scripts/setup-runtime.sh
uv run python main.py server
```

## Verify

```bash
curl http://127.0.0.1:8080/health
curl http://127.0.0.1:8080/network/egress
```

The egress endpoint requests Cloudflare trace through the real application proxy and requires
`warp=on/plus`. The Settings page also provides a verification button. Console authentication applies
when `AISTUDIO_WEB_PASSWORD` is set. WARP does not guarantee an exit country or a static IP.

## Overrides

- `AISTUDIO_WARP_ENABLED=1` (default), `AISTUDIO_WARP_PORT=1080` (loopback only).
- `AISTUDIO_WARP_DIR`: persistent registration/profile directory, default `data/warp`.
- `AISTUDIO_WARP_PROFILE`: existing WARP/WireGuard profile; skips registration.
- `AISTUDIO_WARP_ENDPOINT`: override the profile's UDP endpoint.
- `AISTUDIO_WARP_START_TIMEOUT=60`: fail startup if real egress cannot be verified.
- Explicit `AISTUDIO_PROXY` overrides embedded WARP; ambient `HTTPS_PROXY` does not.
- For intentional direct access set `AISTUDIO_WARP_ENABLED=0` and unset proxy variables.

The default signer is local (`waa.active_server: local_spidermonkey`), with no automatic remote fallback
in WARP mode. Explicit remote signer selection is supported, but its own Google egress is not controlled
by the local WARP tunnel. Existing `data/accounts/` credentials remain usable; old browser profiles are not needed.

See [the Chinese guide](README.md) and [architecture/migration notes](docs/BROWSERLESS_WARP.md).
