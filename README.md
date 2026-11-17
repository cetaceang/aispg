# aispg — Browserless AI Studio API + WARP

基于 [DeconstructedCube/aistudio-api](https://github.com/DeconstructedCube/aistudio-api) 的 Gemini API 网关。
**服务不再启动 Chrome、CloakBrowser、CDP、Playwright 或 Selenium。**

```text
Gemini /v1beta API → FastAPI → 本地 WAA 签名器（Go + SpiderMonkey）
                            → httpx HTTP/2 → wireproxy SOCKS5 → Cloudflare WARP → Google
```

保留多账号轮换、流式响应、工具/图片/多模态、Cookie 导入和 Web 管理面板。
签名、账号探活、Cookie 续期和推理默认共享 WARP 出口。WARP 失败时拒绝启动/请求，不静默直连。

## Docker（推荐）

```bash
cp .env.example .env
docker compose up -d --build
docker compose logs -f
```

镜像构建会编译本地 WAA 签名器，并内置 SHA-256 校验过的 `wireproxy 1.1.3` 和 `wgcf 2.3.1`。
运行时无需 root、`--privileged`、`NET_ADMIN` 或 `/dev/net/tun`；只需要能访问 Cloudflare WARP 的 UDP 端点。
默认仅发布 `127.0.0.1:8080`，访问 <http://localhost:8080> 后在「账号管理」导入 Cookie。

首次启动自动注册 WARP 设备（wgcf `--accept-tos`），将账户、私钥和配置保存至 `data/warp/`，以后复用，不重复注册。
`./data:/app/data` 持久化全部账号、WARP 身份及统计；不要删除该目录来重启服务。
首次下载工具和注册 WARP 必须通过现有网络引导；**业务请求在验证 WARP 出口后才开始**。

已有镜像部署可用 `docker compose -f docker-compose.deploy.yml up -d`；该文件指向本仓库镜像，
不是不含 WARP 的上游镜像。须先构建/发布本次版本，再使用该部署文件；尚未发布时使用上面的 `--build`。
可用 `AISTUDIO_IMAGE` 选择镜像标签。

## 本地运行

需要 Python 3.11+、uv，以及用于构建本地签名器的 Go 1.27+ 和 zig（也支持 clang/gcc）。

```bash
bash scripts/setup-runtime.sh
uv run python main.py server --port 8080
```

工具自动下载支持 Linux x86_64/ARM64、macOS Intel/Apple Silicon 和 Windows x64。
Termux 可使用静态 Linux ARM64 WARP 工具，不需要 proot 浏览器环境。
Windows 可先用 `waa-signer/build.ps1` 构建签名器，再 `uv sync` 和启动 Python 服务。
也可通过 `AISTUDIO_WIREPROXY_BINARY` / `AISTUDIO_WGCF_BINARY` 使用预安装工具。

## 验证实际出口

```bash
curl http://127.0.0.1:8080/health
curl http://127.0.0.1:8080/network/egress
```

`/network/egress` 使用应用真实 SOCKS5 代理请求 Cloudflare trace，要求返回 `warp=on` 或 `warp=plus`，
而不是仅检查本地端口是否监听。示例：

```json
{"enabled":true,"mode":"warp","warp":"on","ip":"104.28.x.x","loc":"US"}
```

设置 `AISTUDIO_WEB_PASSWORD` 后该接口需要控制台登录；也可在「系统设置 → Cloudflare WARP 出口」点击验证。
WARP 不保证国家或固定 IP；Google 是否支持当前出口地区仍需实际请求验证。

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `AISTUDIO_WARP_ENABLED` | `1` | 默认启动内置 WARP |
| `AISTUDIO_WARP_PORT` | `1080` | SOCKS5 仅监听 `127.0.0.1`，不发布代理端口 |
| `AISTUDIO_WARP_DIR` | `data/warp` | WARP 持久化目录 |
| `AISTUDIO_WARP_PROFILE` | 自动生成 | 使用已有 WireGuard/WARP 配置，跳过注册 |
| `AISTUDIO_WARP_ENDPOINT` | profile 中的端点 | 如网络屏蔽 UDP，可显式覆盖地址/端口 |
| `AISTUDIO_WARP_START_TIMEOUT` | `60` | 出口就绪检查时间，超时拒绝启动 |
| `AISTUDIO_PROXY` | 内置 WARP SOCKS5 | 显式设置时覆盖内置 WARP |
| `AISTUDIO_WEB_PASSWORD` | 未设置 | 管理控制台密码 |
| `AISTUDIO_PORT` | `8080` | HTTP 端口 |

环境中的普通 `HTTPS_PROXY` 不会意外覆盖默认 WARP。确需直连时，显式设置 `AISTUDIO_WARP_ENABLED=0`
并取消 `AISTUDIO_PROXY`、`HTTP_PROXY`、`HTTPS_PROXY` 等代理环境变量。

默认 `config.yaml` 的 `waa.active_server` 为 `local_spidermonkey`。本地 WAA 的 Google 请求同样走 WARP，
不会因为本地签名器缺失而悄悄改用远程节点。可在控制台**显式**选择远程 WAA；远程节点自行访问 Google 的出口
不受本机 WARP 控制，因此默认使用本地节点。

## API

```bash
curl http://127.0.0.1:8080/v1beta/models
curl http://127.0.0.1:8080/v1beta/models/gemini-3.5-flash-lite:generateContent \
  -H 'Content-Type: application/json' \
  -d '{"contents":[{"role":"user","parts":[{"text":"Reply with OK"}]}]}'
```

在 Web 控制台配置 API Key 后，附加 `x-goog-api-key` 请求头。
流式请求使用 `:streamGenerateContent?alt=sse`。已有 `data/accounts/` Cookie 账户可沿用，无需浏览器 profile。

## 开发与来源

```bash
uv sync --extra dev
uv run ruff check .
uv run ruff format --check .
uv run pyright src tests
uv run pytest
cd web && bun install --frozen-lockfile && bun run lint && bun run type-check && bun run build
```

无浏览器核心基于上游提交 `fcdc53ee753a9e160686193ac9841bdf97b7227f`（2026-11-17），
本仓库增加内置 WARP 生命周期、默认出口策略、验证接口和部署集成。
详细架构与迁移说明见 [docs/BROWSERLESS_WARP.md](docs/BROWSERLESS_WARP.md)。
Android 伴侣端仅用于用户交互式获取 Cookie，不属于网关服务运行依赖。
