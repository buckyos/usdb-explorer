# 部署模式、访问地址与 HTTPS

[返回手册首页](README.md) · [安装流程](installation.md) · [公网回访超时](troubleshooting.md#外网正常但服务器上的-check-超时)

## 两组独立的模式

`local-node + bundled` 是两个配置项的组合，不是一个 CLI 参数，也不是基础/完整浏览器模式。
无论选哪种组合，上游都必须满足完整 Explorer 的 archive 和私有 tracing 要求。

| 配置项 | 值 | 控制的事情 |
| --- | --- | --- |
| `rpc.mode` | `local-node` | Explorer 连接同机节点的 HTTP loopback RPC，默认 `127.0.0.1:8545`；工具为容器生成私有转发通道 |
| `rpc.mode` | `external` | Explorer 连接显式填写的 RPC 地址；这些地址必须从运维主机及 Explorer 容器均可达，通常用于独立查询节点 |
| `ingress.mode` | `bundled` | Explorer 启动并管理内置入口 Nginx `proxy` 容器，统一转发网页、`/api/`、`/rpc`、`/network.json` |
| `ingress.mode` | `external` | Explorer 不启动入口 `proxy`，由管理员在现有 Nginx 中安装生成的路由片段，并管理域名、监听和证书 |

这两组模式可自由组合。例如，同机节点加已有网站入口使用 `local-node + external`；
远端私网查询节点加内置入口使用 `external + bundled`。`rpc.mode=external` 不表示公开节点 RPC。
`ingress.mode=external` 也不表示上游节点必须在另一台机器。

首次安装默认为 `local-node + bundled`；升级保留原配置。旧配置缺少 `rpc.mode` 时按 `external`
处理，不能只把 RPC URL 改成 loopback 而省略模式。`local-node` 要求本机 rootful Linux Docker。

## 地址与端口参数

| 参数或字段 | 含义与影响 |
| --- | --- |
| `configure --local-node` | 设置 `rpc.mode=local-node`；保留现有同机 RPC 地址，首次切换时使用默认 loopback 地址；不会启用节点的 archive/tracing |
| `--rpc-url` | 同时设置同机 read/trace/broadcast RPC，必须是 HTTP loopback 地址；不改变浏览器入口 |
| `--explorer-url` / `ingress.explorer_url` | **访问者实际使用的 HTTP(S) origin**，包括外部端口；用于前端 API 地址、页面地址、钱包 `/rpc` 元数据和入口域名/HTTPS 跳转 |
| `--http-port` / `ingress.http_port` | **宿主机**发布的 bundled HTTP 端口，默认 28080，转到 Nginx 容器的 8080；HTTPS 模式下用于重定向 |
| `ingress.https_port` | 宿主机发布的 bundled HTTPS 端口，默认 28443，转到容器的 8443；仅在 URL 为 HTTPS 时使用，目前无对应 CLI 选项 |
| `--bind-address` / `ingress.bind_address` | 宿主机监听的 IPv4 地址，不是访问者地址；private 和 external ingress 都要求 loopback |
| `ingress.exposure` | `private` 或 `public`；非 loopback 的 `--explorer-url` 会选择 public 并默认绑定 `0.0.0.0`；工具不会据此创建防火墙或 NAT 规则 |

`--explorer-url` 不是单独的 Nginx 参数：前端也会把 API 请求发送到这个地址。它必须是 origin，
不能带 `/explorer` 等子路径。公网访问不能填写 `127.0.0.1` 或文档示例 IP。

例如，路由器将公网 TCP 38080 转发到服务器 28080 时，公布 URL 要带 `:38080`，
`--http-port` 则仍是 28080。只有两端端口相同时，这两个数字才相同。

当前 `configure` 是同机部署的快捷命令，要求 `--local-node`；提供 `--explorer-url` 时还会选择
`ingress.mode=bundled`。它不是通用模式编辑器，没有 `--bundled`、`--external`、`--https-port`
或证书参数。使用 external ingress 时直接编辑源 JSON，避免用该快捷命令意外切回 bundled。

## 使用域名和 HTTP

同机节点继续使用内置 Nginx，只将 IP 改为域名时，可以使用原命令：

```bash
read -r -p 'Actual HTTP Explorer URL (including any external port): ' explorer_url
usdb-explorer configure --local-node --explorer-url "$explorer_url" --http-port 28080
```

先由管理员将真实域名解析到入口 IP，配置需要的端口映射；工具不会修改 DNS。
然后按下文[应用配置](#应用配置与证书续期)重新生成部署。仅修改 DNS、源 JSON 或转发规则，
不会更新正在运行的前端 API 地址。

## 内置 Nginx 提供 HTTPS

首次开启 bundled HTTPS 时，不能只执行 `configure --explorer-url https://...`：校验还要求
已有证书目录，而当前 CLI 没有证书参数。备份 `~/.config/usdb-public/config.json` 后，
只修改其中的 `ingress` 对象，保留 `rpc`、`deployment_id`、网络和资源配置。
以下是字段示例，域名和证书路径必须替换为实际值，不要把片段覆盖到整个配置文件：

```json
{
  "mode": "bundled",
  "exposure": "public",
  "explorer_url": "https://explorer.example.com",
  "bind_address": "0.0.0.0",
  "http_port": 28080,
  "https_port": 28443,
  "tls": {
    "certificate_dir": "/home/usdb/.config/usdb-public/tls"
  }
}
```

上述例子对应外部 TCP **443 → 宿主机 28443**，以及用于 HTTP 跳转的 **80 → 宿主机 28080**。
入口直接使用宿主机 80/443 时，分别把 `http_port`、`https_port` 设为 80、443，并确认没有其他
服务占用。若外部 HTTPS 使用非标准端口，`explorer_url` 必须包含该外部端口。

证书目录中需要可读取的 `fullchain.pem` 和 `privkey.pem`；证书须匹配实际域名，客户端须信任
签发 CA。目录以只读方式挂入 Nginx，私钥权限只授予必要账号。证书路径不能含 `$`、冒号或换行；
证书文件不能通过符号链接指向目录外。Certbot 的 `live/` 文件通常是此类链接，不能只挂载该子目录。

工具使用已有证书，不申请证书、配置 DNS、防火墙或续期任务。生成的 Nginx 不提供 HTTP-01
challenge 路由；使用已有证书设施或 DNS-01，并通过续期 deploy hook 将证书对更新到专用目录。
HTTP 请求会重定向到公布的 HTTPS origin；本机健康检查也应使用正确的 HTTPS 域名与受信任证书。

## 现有 Nginx 提供 HTTPS

如果宿主机已有 Nginx 管理域名和证书，可以保留 `rpc.mode=local-node`，只把 `ingress` 改为：

```json
{
  "mode": "external",
  "exposure": "public",
  "explorer_url": "https://explorer.example.com",
  "bind_address": "127.0.0.1",
  "web_port": 28080,
  "gateway_port": 28081
}
```

该模式不使用 `http_port`、`https_port`，也不能保留 `ingress.tls`；证书归外部 Nginx 管理。
`web_port` 只服务前端，`gateway_port` 服务公共 RPC 和浏览器 API；二者仅发布在宿主机 loopback。
按[应用配置](#应用配置与证书续期)重新 prepare 后，将生成的 `nginx.locations.conf` 安装到该域名
的 Nginx `server` 内，检查配置并 reload。具体命令见[外部 Nginx 参考](../../explorer/README.md#3-接入现有-nginxexternal-模式)。
在入口接入完成之前，`check` 不能通过。

必须安装完整路由片段，不能只把整个域名代理到 frontend，也不能把 `/api/` 绕过网关直连 backend。
当前 external ingress 强制 loopback 绑定，适用于宿主机 Nginx；独立桥接容器或另一台服务器
无法用其自身的 `127.0.0.1` 访问这些端口。此类拓扑需要单独设计受控连接，不能直接照抄配置。

## 应用配置与证书续期

使用原运维账号备份、编辑源配置；不要改 `default/` 中受摘要保护的生成文件。
已有部署按以下顺序应用地址、模式、监听端口或证书目录变更：

```bash
usdb-explorer down
usdb-explorer prepare --replace
usdb-explorer preflight
usdb-explorer up
usdb-explorer check
```

首次部署使用普通 `prepare`。自定义安装保持原 `--config` 和 `--state-dir`。
此流程重启 Explorer，保留数据库卷和凭据，不重启 usdb-chain；浏览器随后强制刷新，
从实际访问网络验证页面、区块/交易 API 和 `/rpc`。

bundled 模式仅在**同一证书目录内更新证书内容**时，可执行：

```bash
usdb-explorer reload-proxy
```

它先运行 `nginx -t`，通过后 reload，不代替域名、端口、模式或证书目录变更的 prepare 流程。
external 模式由管理员检查并 reload 外部 Nginx，`reload-proxy` 不管理它。

## 公网地址与本机检查

`check` 从执行命令的主机访问公布 URL。公网访问正常并不保证内网能经路由器回访同一个公网
IP；NAT 回环、防火墙或转发规则可能造成超时，且可能是间歇性的。
不要为了本机检查而把公布 URL 改回 loopback，这会让外部浏览器请求它自己的本机地址。

使用域名时，可通过分离 DNS 让内网解析到服务器内网地址、外网解析到公网地址，保持同一域名
和证书。DNS 只改变目标 IP，不改变 URL 的端口：例如公网 443 转发到本机 28443 时，内网直连
也需要在 443 提供对应入口，否则仅修改解析仍然不通。IP 字面量不使用 DNS，不能这样解决。

本机入口诊断与外部验收的命令、v0.2.4 的限制和连接计时方法见
[外网正常但服务器上的 check 超时](troubleshooting.md#外网正常但服务器上的-check-超时)。
