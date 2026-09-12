# USDB 浏览器与公共 RPC 独立部署

此目录提供独立的 `usdb-explorer` 工具和发布通道，并保留 `usdb-public` 兼容命令。
新安装默认连接同机 USDB 的 `http://127.0.0.1:8545`，与 `usdb-node` 默认 Docker 部署配合使用。
也支持在独立机器上通过显式私网 RPC 地址连接远端节点，无需安装节点工具或 clone 源码。
本工具不启动、停止、重配上游节点，不读取 `node.env`，不加入节点的 Docker network。

## 当前范围与发布状态

服务包括 Blockscout backend/frontend、PostgreSQL、Redis 和 Go RPC 网关。Nginx 可选。
网络身份来自 `networks/usdb-testnet-v0.json`，来源 USDB commit 与摘要由相邻的契约文件锁定；
工具的版本是 `vX.Y.Z`，与节点 `usdb-testnet-v0-rN` 分开升级，网络 chain ID 不变。

测试网镜像安全结果采用 `report-only`，允许显式设置 `ingress.exposure=public`，使用 HTTP IP＋端口预览或 HTTPS。
`qualified_for_public_exposure=false` 继续记录尚未完成的镜像验收，不会被自动改为已通过。
新版本构建扫描完整镜像 lock；High/Critical 漏洞记录在 artifact 中，扫描和证据错误仍阻断发布。
准备和启动服务时会提示未完成验收；默认配置仍为 private。外部 Nginx 模式必须使用 loopback
后端绑定；bundled 公网模式可以监听公网地址，选择 HTTPS 时须配置证书。
升级到受维护镜像、实际 archive 重放、重组恢复、SourceDAO 合约验证、外部钱包发送交易仍需验收。
主网尚不支持；测试网的宽松模式不会自动适用于主网。

## 1. 安装与初始配置

目标环境：Linux amd64、Python 3.11+、Docker Engine 25+、Docker Compose v2+。
独立机器建议至少 8 GiB 内存作为低并发预览起点；默认公共服务预算 6 GiB，不包含上游 archive。
磁盘需求会随着 Blockscout 索引数据增长，应单独监控，不能沿用 USDB 节点快照大小估算。

每个 release 都生成对应的一键安装脚本，GitHub Release 正文会提供该版本的完整命令。
使用运行 explorer 的普通运维用户执行，默认无需 sudo。例如版本 `0.2.0`：

```bash
bash <(curl -fsSL https://github.com/buckyos/usdb-explorer/releases/download/v0.2.0/install-usdb-explorer-v0.2.0.sh)
```

示例版本用于说明命名，不表示该 release 已发布。脚本内置该版本安装包的 SHA-256，
通过 HTTPS 下载并核对摘要、release manifest 及包内文件后，再原子切换命令入口。
GitHub Release 同时保留安装包、脚本及各自 `.sha256`，便于离线检查。

| 安装内容 | 默认位置 |
| --- | --- |
| 各版本独立目录 | `~/.local/share/usdb-public/releases/usdb-explorer-vX.Y.Z` |
| 当前版本指针 | `~/.local/share/usdb-public/current` |
| 命令入口 | `~/.local/bin/usdb-explorer`，兼容 `~/.local/bin/usdb-public` |
| 初始配置 | `~/.config/usdb-public/config.json` |

首次安装创建权限为 0600 的配置文件；重复安装或升级保留已有配置、部署数据和旧版本。
下载或校验失败不会切换到不完整版本，也不会静默覆盖同版本中已被修改的文件。
安装只负责工具和初始配置，不启动容器、不重启上游节点，也不自动安装系统依赖。
如果 `~/.local/bin` 尚未加入 PATH，按安装输出执行一次 `export PATH="$HOME/.local/bin:$PATH"`，
或者直接调用 `~/.local/bin/usdb-explorer`。安装器不会自动改写 shell 启动文件。

安装脚本支持 `--install-root`、`--bin-dir` 和 `--config-file`；默认路径满足普通单用户部署。
例如在一键安装命令之后追加 `--install-root /data/usdb-public` 可调整版本存储位置。
已有 `usdb-explorer` 或 `usdb-public` 命令若不属于该安装目录，会报错并保留它，不会覆盖其他工具或手工安装。

源码开发时可直接运行 `explorer/usdb-explorer`，此时仅网关在目标 Docker 中从源码构建；
正式发布包直接使用冻结 digest 的预构建网关，不需要 Go 工具链。

### 同机测试网：默认入口与端口映射

首次安装的默认配置是 `rpc.mode=local-node`、`ingress.mode=bundled`，浏览器入口为
`http://127.0.0.1:28080`。同机默认 USDB RPC 不需要额外填写；执行下一节的 prepare/preflight/up 即可。
公网或局域网通过 IP＋端口访问时，先设置访问者实际使用的 URL。例如把外部 `38080`
映射到测试机 `28080`（`192.0.2.10` 请替换成测试机实际对外 IP）：

```bash
usdb-explorer configure --local-node \
  --explorer-url http://192.0.2.10:38080 --http-port 28080
usdb-explorer prepare
usdb-explorer preflight
usdb-explorer up
```

命令会使用内置 Nginx，并将其绑定到 `0.0.0.0:28080`。在路由器、云平台或宿主机配置
**TCP 外部 38080 → 测试机 28080**；工具不自动修改防火墙或端口映射。没有转发时，URL
中的端口与 `--http-port` 使用同一个值即可。只映射浏览器的这个入口，不映射节点的 8545。
浏览器 API、钱包元数据及 `/rpc` 都使用 `--explorer-url`，因此不能给外部访问者填写 `127.0.0.1`。

已安装 v0.2.2 或更早版本时，先安装包含本功能的新版本。升级会保留原配置；需要显式切换：

```bash
usdb-explorer configure --local-node \
  --explorer-url http://192.0.2.10:38080 --http-port 28080
usdb-explorer down
usdb-explorer prepare --replace
usdb-explorer preflight
usdb-explorer up
```

如果从未 prepare 过，使用普通 `prepare` 即可。`configure` 会备份输入配置，保留 deployment ID、
网络、采样交易和其他设置；只有提供 `--explorer-url` 才切换到 bundled 入口。它不修改已准备的
部署或启动容器，`prepare --replace` 才应用变化并保留数据库凭据、volume。自定义配置继续使用
`--config`，各生命周期命令继续传入原 `--state-dir`。本地 RPC 改过端口时可加
`--rpc-url http://127.0.0.1:自定义端口`；此选项同时更新 read/trace/broadcast。

`local-node` 使用两个固定 digest 的小型 Nginx 转发容器：`rpc-host` 访问宿主机 loopback，
只监听私有共享 volume 内的 Unix socket；`rpc-relay` 在 Explorer 的内部 RPC 网络接入该 socket。
只有 backend/gateway 加入该内部网络，frontend/proxy 不加入；没有新增宿主机 RPC 监听端口，
无需修改或重启 USDB。此模式要求本机原生、非 rootless 的 Linux Docker Engine；远端 Docker、
Docker Desktop 或独立 RPC 服务器使用 `rpc.mode=external`。

更多设置可编辑 `~/.config/usdb-public/config.json`：

| 设置 | 含义 |
| --- | --- |
| `deployment_id` | 独立 Compose project 名；同一数据库部署保持不变 |
| `network` | 包内经过冻结的网络，目前支持 `usdb-testnet-v0` |
| `rpc.mode` | 新安装为 `local-node`；旧配置省略此项仍按 `external`，不会自动改上游 |
| `rpc.read_url` | 历史状态查询端点，Blockscout 与公共只读 RPC 使用 |
| `rpc.trace_url` | 私有 tracing 端点，省略时使用 read_url |
| `rpc.broadcast_url` | 接收已签名交易的端点，省略时使用 read_url |
| `rpc.reference_url` | 可选独立同步参考节点；配置后要求 archive 达到其观察高度和哈希 |
| `rpc.historical_block` | 历史状态采样高度；省略时为观察头部减 256，最低 0 |
| `rpc.transaction` | 已上链交易，用于 receipt/callTracer 采样；省略时最多向前查找 32 块 |
| `ingress.explorer_url` | 浏览器的完整 HTTP(S) origin；钱包 RPC 自动为该 origin 加 `/rpc` |
| `ingress.mode` | `external` 使用现有入口；`bundled` 启动内置 Nginx |
| `ingress.exposure` | 默认 private；测试网 public 支持临时 HTTP IP＋端口和 HTTPS |
| `ingress.http_port` | bundled 模式在本机发布的 HTTP 端口，默认 28080；与 URL 中的映射端口独立 |
| `resources.other_services_memory_gib` | 同机默认 `auto`，按其他运行容器的内存上限计入预算；也可手动声明整数 GiB |

样例内区块 35 的交易是当前 testnet-v0 已知的 SourceDAO 部署交易。网络重置后需重新选择样本，
工具会拒绝不属于当前 canonical chain 的交易，不能用空 trace 或空 receipt 代替验收。

`external` 模式的 RPC URL 需从运维主机及 Docker 容器均可到达，优先使用受控私网 DNS/IP。
`local-node` 的 read/trace/broadcast 使用宿主机 HTTP loopback 地址，容器地址由工具转换。
仅把旧配置中的 RPC 改为 `127.0.0.1` 而不设置 local-node，会让容器连接到自身；
`host.docker.internal` 也无法直接访问仅监听宿主机 loopback 的端口。
当前支持 HTTP(S)、系统可信 CA，不接受 URL 用户名、密码或 query；需要鉴权时使用受控网络代理。
tracing 上游必须提供基本身份和区块查询接口，但不会因此暴露到公共 RPC 网关。

配置和上游路径按私有运维资料保存，实际 RPC 地址不提交 Git。

## 2. 准备、检查与启动

```bash
usdb-explorer prepare
usdb-explorer preflight
usdb-explorer up
usdb-explorer status
usdb-explorer logs --follow
```

默认状态目录为 `~/.config/usdb-public/default`；每个命令均可通过 `--state-dir` 指定同一自定义目录。
多套部署使用不同目录和 `deployment_id`。每个部署只使用一个状态目录作为操作入口。
`prepare` 默认读取安装器创建的 `~/.config/usdb-public/config.json`，可通过 `--config` 使用其他配置文件。

`prepare` 不连接 Docker 或 RPC，只校验配置并生成文件；已存在的目录不会被无条件覆盖。
文件包括配置、凭据、Compose、镜像锁、公开钱包信息、Nginx 配置和文件摘要。
数据库凭据和状态目录分别使用 0600、0700 权限，不能把整个状态目录放入 Git。
生成文件由摘要保护，不要手工修改 `compose.json` 或 `nginx.conf`，应编辑输入配置后执行替换流程。

`up` 先检查 Docker 主机资源、数据库身份、上游能力，再启动自身服务。同机模式先启动内部转发容器，
核对容器侧三个 RPC 路由的网络身份、同步状态与同一 canonical checkpoint，通过后才启动浏览器。
转发检查失败时不会继续启动浏览器；已启动的转发容器可通过 `logs` 检查，用 `down` 停止。
Compose project 内只有浏览器服务，没有 archive 或 miner；启动成功不等于索引已经追平。
历史查询、tracing 或网络身份不满足要求时会报错，不会自动关闭这些功能掩盖缺口。

## 3. 接入现有 Nginx：external 模式

将 `ingress.mode` 设为 `external` 时，不创建入口 Nginx 容器。frontend 绑定 `127.0.0.1:28080`，网关绑定 `127.0.0.1:28081`。
浏览器域名必须经过完整路由才能使用，直接访问 frontend 端口不是完整浏览器入口。
可将 `explorer_url` 设为 `http://127.0.0.1:28082`，用作现有 Nginx 的本机预览入口：

```nginx
server {
    listen 127.0.0.1:28082;
    server_name localhost;
    include /etc/nginx/usdb-public.locations.conf;
}
```

将生成的 `nginx.locations.conf` 复制到管理员选定的 include 位置，再由管理员检查、reload：

```bash
sudo install -m 644 ~/.config/usdb-public/default/nginx.locations.conf /etc/nginx/usdb-public.locations.conf
sudo nginx -t
sudo systemctl reload nginx
```

正式域名的 server block、80/443、证书和续期由现有 Nginx 管理。只需将 `explorer_url` 设置为
实际 HTTPS origin 并重新准备服务；无需将前端或网关端口暴露到公网。
配置片段转发 `/rpc`、`/api/`、`/network.json`，其余为 frontend；所有 API 均经过网关，
不允许把 `/api/` 直接代理到 Blockscout backend。`/socket` 暂未开放，不能据此宣称支持 WebSocket。

若现有 Nginx 运行在另一容器内，其 `127.0.0.1` 同样不是宿主机：应选定受控的私网绑定地址，
将配置片段的上游地址用于那个可达接口，并配置宿主机访问控制。当前 private 预览仅允许 loopback。
服务不会自动修改服务器上其他 www 的配置，也不会通过 `reload-proxy` 重启外部 Nginx。

## 4. 使用内置 Nginx：bundled 模式

本机 HTTP 预览的 ingress 可以简化为：

```json
{
  "mode": "bundled",
  "exposure": "private",
  "explorer_url": "http://127.0.0.1:28080"
}
```

此时仅 Nginx 绑定端口，frontend/gateway 不发布宿主机端口。
使用 HTTPS 时设置 `explorer_url`、`http_port`、`https_port` 和 `tls.certificate_dir`。
证书目录含 `fullchain.pem`、`privkey.pem`，只读挂载到容器；私钥不会复制到发布包或状态目录。
例如私有 HTTPS 预览可使用 `https://explorer.internal:28443`，端口默认 28080/28443。
域名 DNS/hosts 和证书信任须由测试环境正确配置，工具不提供跳过 TLS 校验选项。

测试网 bundled 公网模式使用 `exposure=public`、`bind_address=0.0.0.0`、80/443 和公网域名，
镜像漏洞采用 report-only 并保留未完成验收状态。
HTTP 自动跳转配置中的 HTTPS origin。此版负责使用已有证书与 reload，不自动申请证书、开放防火墙
或建立 ACME 任务；证书签发和续期由目标机器的 Certbot/证书管理服务负责。

证书更新成功后执行：

```bash
usdb-explorer reload-proxy
```

命令先 `nginx -t`，通过才 reload，不重启其他服务。
挂载整个目录允许原子替换证书文件；目录外的符号链接被拒绝。
Certbot 的 `live/` 文件通常链接到其他目录，应在续期 deploy hook 中将证书对安全更新到专用目录，
再调用以上命令；不能仅挂载 `live/某域名` 造成容器内链接失效。
生成配置不内置 HTTP-01 challenge 路由，使用 DNS-01 或已有证书设施，避免占用正在服务的端口。

## 5. preflight、check 与 status 的区别

- `preflight`：只访问配置中的上游 RPC，核对所有端点的 chain ID、network ID、genesis、同步状态、
  固定高度及哈希，执行历史 balance/code/call、交易和整块 callTracer 样本。不需要浏览器已启动。
- `status`：显示自身容器状态和入口地址。容器 running 不能证明已经索引完成。
- `check`：重复上游采样，并通过 `explorer_url` 查询实际网关和浏览器 API，核对固定区块、交易、
  receipt 状态、gasUsed、实际 fee、canonical membership，结束前再次核对哈希。外部 Nginx 尚未接入会失败。

```bash
usdb-explorer check
```

未设置 `reference_url` 时报告明确显示 `not_configured`，只证明与所配置上游一致，不能证明它已达到
全网最新高度。`CHECKED` 也不等于完整 archive 重放、重组恢复、钱包发交易、TLS 部署或奖励语义验收；
这些项目单独报告 `not_run`。公共网关只转发用户自己签名的交易，检查命令从不发送交易。

网关的请求大小、批量、并发和方法限制在两种 Nginx 模式均生效。当前按直连来源限流，忽略客户端
提供的 forwarded headers；经同一代理的客户端共享保守配额。外层 Nginx 可自行增加按客户端 IP 的限流，
但不能通过关闭应用网关换取吞吐。区块奖励、供应量和 fee 分账尚未完成 USDB 适配，继续隐藏相关字段。

## 6. 独立升级、停止与恢复

```bash
usdb-public down
# 使用新版本 release 正文给出的一键安装命令，例如：
bash <(curl -fsSL https://github.com/buckyos/usdb-explorer/releases/download/v0.2.0/install-usdb-explorer-v0.2.0.sh)
usdb-explorer prepare --replace
usdb-explorer up
usdb-explorer check
```

`down` 只停止自身 Compose project，保留数据库 volumes。`prepare --replace` 要求本部署已停止，
保留原凭据并将旧配置目录备份到带时间戳的相邻目录。配置备份也是私有资料，不提交 Git。
工具升级、浏览器升级、上游节点升级各自独立；配置 schema 和上游能力仍须相容。
安装新版本只切换工具入口，不会自动更新正在运行的容器；上面列出的完整流程才会应用新服务版本。
自定义部署继续传入原有 `--state-dir`、`--config` 和安装路径，保持同一个 `deployment_id`。

数据备份至少包含私有状态目录、Postgres 一致性备份和 backend-data。更换主机时先恢复数据及凭据，
可用 `prepare --credentials-file /secure/saved/credentials.json` 为新状态目录生成对应配置。
已有数据库 volume 的网络/凭据指纹不匹配会阻断 up。新目录不能用随机生成的密码接管旧数据库。
数据库升级可能运行 schema migration，配置备份不是数据库备份，回退镜像前须核对数据兼容性。

同机部署默认使用 `other_services_memory_gib="auto"`：每次 up 汇总其他**运行中**容器的内存上限，
向上取整为 GiB，连同 Explorer 预算和系统余量校验；手动整数预算仍须覆盖这些上限。
这不包含非 Docker 进程或未来启动的容器；这类负载应手动预留预算。其他容器未设置内存上限会拒绝共置，
内存不足也不会自动降低节点资源或检查标准。USDB 节点如需缩小自己的预算，可独立使用通用的
`usdb-node set-resource-policy --mode auto --external-memory-budget 6g`，按节点原有流程在停止期间调整。
该选项不识别 explorer，也不控制它；管理员负责在后续调整时继续保持整机预算一致。
不要将“当前 RSS 很小”作为永久资源预算。独立机器无需执行任何 `usdb-node` 命令。

## 7. 独立发布与维护

本工程的 `ci.yml` 测试 gateway、部署、安装迁移、网络契约和隔离 Nginx；
`release-build.yml` 在收到 `vX.Y.Z` annotated tag 后构建镜像与草稿；
`release-publish.yml` 校验原始附件后发布为 Pre-release，并检查匿名下载。

在已提交并推送、与 origin/main 一致的干净 checkout 中执行：

```bash
python3 scripts/prepare_release.py --version 0.2.1
python3 scripts/prepare_release.py --version 0.2.1 --create --push
```

tag 工具只访问本仓库。默认预检；`--create` 创建 annotated tag，`--push` 才会推送。
失败时保留已有 tag，按报错续推；不要移动或复用版本。
构建成功后，从 Actions 的 **USDB Explorer Publish** 的 **Use workflow from** 选择对应 tag
（如 `v0.2.1`），无需再填写版本：

```bash
gh workflow run release-publish.yml --repo buckyos/usdb-explorer --ref v0.2.1
```

发布 environment 为 `usdb-explorer-release`，须允许 `v*` tag，与节点发布配置分别管理。
已有 `v0.2.0` 不包含 tag 选择修复，过渡方式见 [发布与迁移](../docs/release-and-migration.md)。
完整构建包含七个镜像的安全扫描；扫描失败时即使草稿已存在也不能 Publish。
维护者可运行 `release-security-review.yml`，选择精确的 release tag，并提供该版本 gateway digest，
以 `report-only` 收集新证据，或以 `strict` 阻断尚未解决的 High/Critical 漏洞。
新版本随安装附件发布变更 JSON、checksum 和 Markdown；正文列出本版变化、升级操作和提交范围。
草稿下载链接返回 HTTP 404，发布后全部七个附件 URL 的匿名下载和 SHA-256 检查通过才报告成功。
旧四附件版本保持兼容。变更记录规则见 [发布变更管理](../docs/release-change-management.md)。
Publish 复用原附件并验证 source revision、network contract、installer、image lock，不重新打包，
不占用 Latest。发布后下载验证失败可重跑同一 Publish；原附件保持不变。

## 8. 从 usdb-public 迁移

旧 `buckyos/usdb` 的 `usdb-public-v0.1.0` tag 和附件继续保留，新版本在本仓库发布。
新安装脚本生成两个命令：`usdb-explorer` 与兼容的 `usdb-public`，共同指向当前版本。
为了识别已有安装和数据库，配置/部署 schema 与默认路径仍使用 `usdb-public` 名称：

- 配置：`~/.config/usdb-public/config.json`
- 部署：`~/.config/usdb-public/default`
- 安装：`~/.local/share/usdb-public`

安装新版本保留旧版本目录和原配置；不会停止、启动或重建容器。
需要应用新的部署配置或镜像时，按原升级流程备份数据库，`down` → `prepare --replace` → `up`。
保留 `deployment_id`、数据库凭据、Compose project 和 volume 名称；不要因工程改名而手动更改它们。

## 9. 网络契约与上游资格

`explorer/networks/usdb-testnet-v0.contract.json` 固定来源 USDB commit、catalog SHA-256 和 RPC profile。
普通构建只读本仓库内的契约文件，不读取其他 checkout。
更新方法见 [网络接口](https://github.com/buckyos/usdb-explorer/blob/main/docs/network-contract.md)。

本地生成开发包：

```bash
python3 explorer/package_release.py --check-network
python3 explorer/package_release.py --version 0.2.0 \
  --gateway-image 'ghcr.io/buckyos/usdb-explorer-gateway@sha256:<真实构建digest>' \
  --output-dir /secure/explorer-release
```

未提交的开发包标记 `source_dirty`，不能通过发布校验。所有第三方镜像仍沿用迁移前的
私有兼容性预览基线；拆仓不改变已完成或未完成的 archive、reorg、钱包及镜像验收。
