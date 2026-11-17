# 无浏览器与 WARP 迁移

## 来源与执行路径

浏览器到纯 HTTP 的重构基于 DeconstructedCube/aistudio-api 的提交
`fcdc53ee753a9e160686193ac9841bdf97b7227f`。同步了相互依赖的会话、Wire 编解码、账号代际隔离、
HTTP 流式传输、WAA 签名、本地 Go 签名器、前端及对应测试，避免混用旧浏览器状态机。
保留本仓库的 Docker 环境配置读取、端口默认仅绑定 localhost，以及原有模型规则。

旧 `BrowserSession`、CDP 客户端、浏览器 JS 注入/回放、浏览器安装脚本与依赖已删除。
`AIStudioClient` 实际构造 `GatewaySession`，后者使用 `HTTPStreamTransport` 和 `WaaSigner`。
SpiderMonkey 是嵌入式 JS 执行引擎及 DOM 宿主模拟，不是 Firefox 浏览器进程。
Go 源码中的 `transport_browser.go` 表示浏览器协议指纹模拟，也不启动浏览器。

## 启动顺序与出口策略

1. 解析出口：显式 `AISTUDIO_PROXY` → 默认内置 WARP →（仅关闭 WARP 后）普通代理环境变量/直连。
2. 校验 SOCKS 端口未被占用；准备 WARP 身份及工具。
3. 启动应用拥有的 wireproxy，使用 SOCKS5 **实际请求** Cloudflare trace，验证 `warp=on/plus` 与有效 IP。
4. 启动本地 WAA 签名器，并传递同一 `settings.proxy_url`。
5. 初始化账号、HTTP 网关及后台 Cookie 轮换。
6. 关闭时先排干/取消后台任务与 HTTP 连接，再停止签名器和 WARP 子进程。

WARP 的 WireGuard 外层 UDP、首次 GitHub 工具下载与 Cloudflare 设备注册是必需的引导流量，
不可能先经过尚未建立的 WARP；业务 Google 请求不在此例外范围。
本地 Python → 本地签名器为 loopback 直连，签名器 → Google 仍使用 WARP SOCKS。
每个签名请求也携带本地 `proxy` 参数，避免复用已存在的签名守护进程时遗漏出口设置。

不在代码中替换系统默认路由，不影响宿主其他项目，不需要 root/TUN。
WARP 断开后，HTTP 客户端仍指向同一个 SOCKS5 地址，失败即报错，不创建直连替代客户端。
没有本地签名器时，默认 WARP 模式拒绝静默切换远程节点。用户可在配置中明确选择远程签名服务，
但远程服务自身的 Google 出口无法由本机 WARP 决定。

## 持久化和故障处理

- `data/warp/wgcf-account.toml`：设备身份，重启复用。
- `data/warp/wgcf-profile.conf`：私钥与 WireGuard profile。
- `data/warp/wireproxy.conf`：只保留网络字段，去掉 wg-quick 的执行命令，固定 SOCKS loopback 绑定。
- `data/warp/bin/`：原生运行时下载的工具；Docker 工具位于 `/usr/local/bin`。
- `data/warp/registration.log` / `wireproxy.log`：本地诊断文件。

目录权限 0700，状态/日志/私钥文件 0600；整个 data 目录不进入 Git 或 Docker 构建上下文。
工具版本及 SHA-256 摘要固定在 `infrastructure/warp/binaries.py`；下载不执行远程 shell，
压缩包只提取预期可执行文件，不允许路径穿越。已有工具可用专用环境变量覆盖。

遇到无法注册可提供已有 `AISTUDIO_WARP_PROFILE`；遇到 UDP 端点阻断可配置 `AISTUDIO_WARP_ENDPOINT`。
WARP 就绪超时或端口冲突会明确阻止服务启动，不隐藏错误。不会自动销毁身份反复注册。

## 实测验收（2026-11-17，Linux ARM64）

- 原生进程仅有 Python、wireproxy 和 waa-signer；签名器 argv 明确包含
  `-proxy socks5://127.0.0.1:1080`，没有浏览器进程。
- 导入测试 Cookie 后，模型列表返回 200；`gemini-3.5-flash-lite` 普通生成与 SSE
  均返回 200、文本 `OK`、`finishReason=STOP`，未出现登录重定向。
- Docker 以 `--user 1001:1001 --cap-drop ALL --security-opt no-new-privileges`
  启动，无 TUN/额外设备；首次自动注册 WARP 并实测 `warp=on`。
- Docker 重启前后 `wgcf-account.toml` 与 `wgcf-profile.conf` 的 SHA-256 不变，
  不重复注册身份，正常退出时签名器及隧道子进程被回收。
- Docker 使用已验证的 WARP profile 后，普通与 SSE 生成同样返回 `OK`。
  **某个新注册出口虽返回 `warp=on`、`loc=US`，Google 仍报 `Region not supported`**；
  Cloudflare trace 不是 Google 地区可用性证明。可通过 `AISTUDIO_WARP_PROFILE` 指定已验证配置；
  地区错误导致账号自动停用后，需在账号管理中重新启用再测试。
- 分别终止原生和 Docker 自有的 wireproxy 后，出口探针返回 503，生成返回
  502 `ConnectError: All connection attempts failed`（Docker 约 0.1 秒），没有直连回退。
- 458 项 Python 测试通过，Ruff、格式校验、Pyright、前端 ESLint/类型检查/构建及 Go 测试通过。

## 验证边界

离线单元测试覆盖默认代理优先级、配置转换、摘要校验、复用身份、启动顺序、失败关闭及资源回收。
真实部署验收还应验证：WARP trace、签名器使用代理、Gemini 普通/流式请求、重启复用身份、
切断 WARP 后无直连回退，以及进程表中不存在浏览器。

上述是真实网络测试，不代表所有 WARP 出口、账号或模型都可用。
测试时 `gemini-3.8-flash` 与 `gemini-3.6-flash` 返回上游 503 high demand；
这与登录失效不同。macOS、Windows、Termux 的原生运行尚未在本环境实测。

`/network/egress` 为受 Web 鉴权保护的实际出口探针，不仅表示配置已填写。
