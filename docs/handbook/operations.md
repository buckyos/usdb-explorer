# 日常运维

[返回手册首页](README.md) · [版本范围](README.md#版本与验证范围)

启用水龙头后还需备份独立的钱包与账本卷。额度、补款、恢复和关闭功能见
[测试网水龙头](faucet.md)；`faucet fund` 会发送转账，其余浏览器检查命令保持只读。

## 预检、容器状态与访问检查

| 命令 | 检查对象 | 不能据此得出的结论 |
| --- | --- | --- |
| `preflight` | 配置中的上游 RPC、网络身份、状态和 tracing | 不访问公布的浏览器 URL，不保证前端或端口转发正常 |
| `version` / `--version` | 当前命令所使用的工具版本、源码 revision 和安装目录 | 工具升级不代表部署已更新 |
| `status` | 工具与已准备部署的版本、网络、入口和容器状态 | 已准备版本不等于容器运行版本；running 不代表完成索引或能够对外访问 |
| `check` | 先做 preflight，再对比本机、主机/LAN、公布 URL 的 `/rpc` 和浏览器 API | 从服务器发起的探测不等于外网访问验收，也不等于全历史、重组或钱包广播验收 |

新版默认先打印明确结论，例如首节点没有交易时：

```text
Preflight PASSED WITH WARNINGS: Explorer may start.
Checkpoint: block 0
Historical state: genesis state readable; archive history not yet verified
Transaction tracing: PENDING (no mined transaction sample)
```

| 结论 | 退出码 | 下一步 |
| --- | --- | --- |
| `Preflight PASSED` | 0 | 可以执行 `up`，启动后再 `check` |
| `Preflight PASSED WITH WARNINGS` | 0 | 可以启动完整浏览器；交易样本验收仍待完成 |
| `Check PASSED` | 0 | 本次入口/API 和采样检查通过，仍需关注索引及其他验收边界 |
| `Check PASSED WITH WARNINGS` | 0 | 入口/API 正常，真实交易验证仍在等待样本 |
| `Preflight FAILED` / `Check FAILED` | 1 | 按第一条明确错误处理，不把重装或重复 up 当作通用修复 |
| 命令参数错误 | 2 | 核对该版本 `--help` |

上述人类可读输出是本分支新增功能。v0.2.4 只显示 JSON：`PREFLIGHT_PASSED` 和
`PREFLIGHT_READY_NO_TRANSACTION_SAMPLE` 均表示可进入启动；后者的 tracing 真实样本尚未验证。
`CHECKED_NO_TRANSACTION_SAMPLE` 表示入口/API 检查通过，但没有完成交易验收。

新版本的脚本调用必须显式加 `--json`，stdout 只输出一个 JSON 文档，失败时退出码仍为 1：

```bash
usdb-explorer preflight --json
usdb-explorer check --json
```

保留成功报告的原 `status` 字段；失败报告使用 `PREFLIGHT_FAILED` / `CHECK_FAILED` 和
`error.category`、`error.message`。自动化若要求交易实际验证，应同时检查 `trace_sample=passed`，
不能只判断退出码为 0。检查命令只读，不会发送交易、开启 mining 或修改上游。

## 区分本机入口与公布地址

安装包含多入口诊断的版本后，直接运行 `usdb-explorer check`，会先独立报告上游预检，再分别检查：

| 输出名称 | 检查入口 |
| --- | --- |
| `loopback` | bundled Nginx 的回环地址和本机端口 |
| `lan` / `lan-2` 等 | 默认路由接口的 IPv4 地址和本机端口；具体绑定某个地址时只检查该地址 |
| `loopback-ipv6` | 启用 bundled IPv6 后检查 `::1` 和本机端口；绑定具体非回环 IPv6 地址时跳过 |
| `host-ipv6` / `host-ipv6-2` 等 | 启用 bundled IPv6 后检查 IPv6 默认路由接口的可用地址；具体绑定时只检查该地址 |
| `configured` | 已 prepare 的 `ingress.explorer_url`，按服务器当前 DNS/路由和 HTTP 代理环境访问 |

每项显示地址、`PASSED` / `FAILED` / `SKIPPED`、耗时，失败时显示独立错误。
每个入口进一步列出 `rpc`（chain ID 可达性）、`api`（区块列表 API 可达性）和 `canonical`
（规范链身份、区块及交易数据核对）。RPC 失败后仍尝试 API；基础请求失败时 canonical 显示
`NOT_RUN`。RPC/API 都能连接而刚出区块的样本 API 返回 404 时，会标为
`EXPLORER_SAMPLE_UNAVAILABLE`，提示稍后重试并检查索引进度，不把它当作连接超时。
上游只预检一次，入口共享同一检查点；某个入口失败不会阻断其他入口的结果收集。
入口请求的单次网络超时为 5 秒，最多并行检查 4 个入口；耗时为该入口完整检查时间，包含 RPC 和 API，
不是单独的 TCP 握手时间。每个入口会进行多个请求，有助于暴露间歇性故障，但一次通过不证明长期稳定。

本机、LAN 都成功而 `configured` 超时时，会提示检查 DNS、HTTP 代理、路由、端口映射、防火墙和
NAT 回环，并要求从外网对照验证；不会直接断言路由器有问题。TLS、HTTP 和索引不一致会分别保留
具体错误。任何实际检查的入口失败，整体仍为 `Check FAILED`、退出码 1，不用本机成功覆盖公网失败。

仅回环绑定时 LAN 显示 `SKIPPED`；具体非回环绑定时 loopback 显示 `SKIPPED`。
IPv6 未启用时不增加 IPv6 检查项；已启用但无法探测到可用主机 IPv6 地址时，`host-ipv6`
显示 `SKIPPED` 并给出原因，不能据此认为公网 IPv6 可达。启用及外部验收见[双栈入口](networking.md#ipv4-与-ipv6-双栈入口)。
地址发现依赖 `iproute2`，只扫描默认路由接口、最多 4 个地址，不遍历 Docker 网桥；发现失败时明确提示。
external ingress 的管理员代理监听端口无法从内部 backend 端口推断，因此自动本机/LAN 检查显示
`SKIPPED`，仍检查公布入口；可用 `--url` 明确指定管理员代理的实际入口。
上游预检失败时，入口的规范链比较显示 `SKIPPED`，不会绕过完整模式要求。

bundled HTTPS 使用本机 `https_port`，保持公布域名的 Host、TLS SNI 和证书验证；不会绕过证书校验，
也不跟随 HTTP 跳转去公网。直接本机/LAN 探测绕过 HTTP 代理环境变量，因此能独立反映本机入口状态。
这只改变该次连接的目标，不修改源配置、前端环境或部署文件。

v0.2.7 及此前的 `check` 默认只检查公布 URL。在支持 `--url` 的版本中，可临时只检查 HTTP bundled 本机入口：

```bash
usdb-explorer check --url http://127.0.0.1:28080
```

若改过监听端口，使用对应端口。该参数不修改源配置、前端环境或已生成文件，仍使用原上游。
输出会明确提示公布地址没有被检查，JSON 的 `ingress_check` 为 `override_origin`。
旧版默认该字段为 `configured_origin`；新版默认是 `multiple_origins`，增加 `upstream`、
`ingress_results` 和 `diagnosis` 字段。成功保留 `CHECKED` / `CHECKED_NO_TRANSACTION_SAMPLE`，
失败保留 `CHECK_FAILED` 和顶层 `error`，并附上各入口结果；每项的 `checks` / `check_errors`
区分可达性与规范链核对。`--json` 始终只输出一个 JSON 文档。
这些结果只证明执行命令的主机能否访问；公网访问仍需从外部网络复核。
HTTPS 仍要求正确证书和主机名；此功能不跳过证书校验，也不自动跟随入口重定向。
v0.2.4 不支持 `--url`，可使用[本机 curl 检查](troubleshooting.md#preflight-成功但-check-超时)。
若外网页面已正常而本机回访公网地址超时，保留实际公网 URL，按
[公网回访排错](troubleshooting.md#外网正常但服务器上的-check-超时)检查 NAT/防火墙，
不要用本机成功覆盖公布地址检查的失败结论。

地址、模式和 HTTPS 的修改见[部署模式与访问地址](networking.md)；证书原路径续期可以
`reload-proxy`，但域名、监听端口或模式变更需要重新 prepare。

## 日常观察

```bash
usdb-explorer version
usdb-explorer status
usdb-explorer check
usdb-explorer logs --follow
```

安装包含版本诊断功能的 release 后，`version`（或 `--version`）无需 Docker 或已准备的部署，
显示当前工具版本、源码 revision 和实际安装目录。开发源码目录明确显示 `development`，
旧包缺少源码 revision 时显示不可用，不根据目录名或镜像标签猜测版本。兼容命令 `usdb-public`
也支持相同用法。

`status` 先显示工具身份和已准备部署的信息：版本、部署目录、deployment ID、网络、Chain ID、
genesis、RPC/入口模式、访问 URL 和水龙头开关；随后用简洁表格显示每个容器的状态、健康检查、
运行时长及已发布端口。完整容器名与镜像引用保留在 JSON 中，不把长镜像摘要放入默认表格。

**工具版本来自安装包的 `release.json`，已准备版本来自部署的 `deployment.json`。**
两者不同时会明确提示，并给出 down → prepare --replace → preflight → up → check 的应用流程。
应先核对目标 release 的升级说明，继续使用原 `--config` / `--state-dir`；status 本身不会更新配置
或重启服务。`Prepared release` 仅说明部署文件由哪个工具版本生成，容器是否存在、是否运行要看
后面的观察结果，不能将该版本号当作运行容器镜像已经核验一致的证明。

脚本可读取以下 JSON 输出，其中不包含数据库凭据、私有 RPC URL 或容器启动命令：

```bash
usdb-explorer version --json
usdb-explorer status --json
usdb-explorer status --state-dir /path/to/deployment --json
```

`version --json` 使用 `usdb-explorer-version:v1`，`status --json` 使用 `usdb-explorer-status:v1`。
status 的 `tool` 与 `deployment` 分别描述工具和已准备配置，`version_relation` 为 `same`、
`different` 或 `unknown`，`containers` 为本次 Docker 观察。主要状态如下：

| status 状态 | 含义 | 退出码 |
| --- | --- | --- |
| `OBSERVED` | 已取得容器观察；即使包含 stopped/unhealthy，也需逐项查看 | 0 |
| `NOT_PREPARED` | 指定部署目录不存在，仍可看到工具版本；核对路径或执行 setup/prepare | 0 |
| `NO_CONTAINERS` | 已准备部署，但 Docker 未返回该部署的容器 | 0 |
| `CONTAINERS_UNAVAILABLE` | Docker 不可用、权限不足、超时或返回格式无效；保留版本和配置摘要 | 1 |
| `FAILED` | 工具或部署校验失败等错误；按 `error` 排查 | 1 |

这些退出码表示能否完成状态查询，不能代替 `check` 的就绪判断。Docker 不可用时不会自动启动
Docker；未准备部署时不会创建配置或目录。已有部署文件校验失败时会报错，不降级为“未准备”。

跟随日志用 Ctrl+C 退出，不会停止服务。记录首次失败的服务、错误分类、时间和版本；观察磁盘、
内存余量、容器重启和索引高度。节点长期不出块时先判断其是否本来就在等待首次 mining；
浏览器超时则按网络/服务故障处理，不能用“没有区块”解释。
测试网的镜像安全 report-only 警告不阻断启动，也不表示生产镜像验收已通过。

## 停止、升级与备份

`usdb-explorer down` 只停止 Explorer 的 Compose project，保留数据库卷和上游节点。
正常续跑使用 `up`。修改配置或更新容器版本时，先备份，再执行：

```bash
usdb-explorer down
# Install the selected Explorer release using its published installer command.
usdb-explorer prepare --replace
usdb-explorer preflight
usdb-explorer up
usdb-explorer check
```

备份至少包括源配置、私有部署目录/凭据、PostgreSQL 一致性备份和 backend-data。
运行中的 PostgreSQL 应使用其支持的一致性导出方案；直接复制活动卷文件不是可靠备份。
停写后做卷快照时，应停止相关写入者并按存储方案保证一致性。部署目录的自动备份不能代替数据库备份。
升级前确认数据库 migration 和回退兼容性，不默认用旧镜像打开新数据库。

使用自定义路径时，始终沿用原 `--config`、`--state-dir`、安装目录和 `deployment_id`。
不要用 `docker compose down -v`、随机生成的新凭据或改 project 名称“修复”旧部署。
恢复到新主机的完整边界见[部署参考](../../explorer/README.md#6-独立升级停止与恢复)。
