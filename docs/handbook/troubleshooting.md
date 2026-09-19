# 故障排查

[返回手册首页](README.md) · [命令结论与退出码](operations.md#预检容器状态与访问检查)

## 按现象查找

| 现象 | 入口 |
| --- | --- |
| preflight 只有 JSON，看不出是否成功 | [预检结果](#预检结果与等待交易) |
| preflight 成功，check 超时 | [检查访问链路](#preflight-成功但-check-超时) |
| HTML 打开，但数据请求失败 | [页面和 API 地址](#页面打开但数据不显示) |
| 高度 35 超过当前链头、旧交易找不到 | [旧采样配置](#旧采样配置) |
| 缺少 archive、tracing 或上游不可达 | [RPC 分类](#rpc-分类与恢复) |
| 内存、Docker、摘要或数据库身份错误 | [部署问题](#部署文件资源与数据库问题) |

## 预检结果与等待交易

v0.2.4 的 `PREFLIGHT_READY_NO_TRANSACTION_SAMPLE` 表示预检允许启动，当前没有真实交易样本；
它不是失败，也不是完整交易追踪验收通过。首节点未挖矿时可以出现 `checkpoint.number=0`、
`historical_state_sample=genesis_only`、`trace_sample=pending_no_transaction_sample`。
新版显示 `PASSED WITH WARNINGS`。有交易后重新运行 `check`，或选择更早的 canonical 交易验证。

**恢复标志**：浏览器入口/API 可用；产生交易后真实 tracing 样本通过。零 peer 在首节点等待
首次 mining 时可以正常；普通加入节点则仍应检查其入网和同步状态。

## preflight 成功但 check 超时

`preflight` 访问节点 RPC；`check` 还访问公布的浏览器入口。即使没有挖矿，genesis 查询也应正常响应。
新版错误会区分 `read_url` / `trace_url`、`ingress.explorer_url /rpc`、`ingress.explorer_url /api/v2`。

先确认当前配置的**实际访问地址**。可看 `usdb-explorer status` 的 Ingress 行，或只读选取必要字段：

```bash
python3 - <<'PY'
import json
from pathlib import Path
config = json.loads((Path.home() / '.config/usdb-public/default/config.json').read_text())
for key in ('mode', 'explorer_url', 'bind_address', 'http_port'):
    print(key, config['ingress'].get(key))
PY
```

不要上传整个配置目录。`192.0.2.10` 是文档示例，不能作为实际入口。
依次检查同机节点和本机 Nginx（自定义端口相应替换；这些命令也适用于 v0.2.4）：

```bash
curl --connect-timeout 3 --max-time 10 -fsS http://127.0.0.1:8545 \
  -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","id":1,"method":"eth_getBlockByNumber","params":["0x0",false]}'
curl --connect-timeout 3 --max-time 10 -fsS http://127.0.0.1:28080/rpc \
  -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","id":1,"method":"eth_getBlockByNumber","params":["0x0",false]}'
curl --connect-timeout 3 --max-time 10 -fsS http://127.0.0.1:28080/api/v2/blocks
curl --connect-timeout 3 --max-time 10 -fsS 'http://127.0.0.1:28080/api/v2/transactions?filter=validated'
```

| 观察 | 处理 |
| --- | --- |
| 节点 RPC 失败 | 检查节点运行、监听地址、端口与资源；先恢复上游 |
| 节点正常，本机 `/rpc` 失败 | 查看 proxy、gateway、rpc-host、rpc-relay 状态和日志 |
| 本机 `/rpc` 正常，API 失败 | 检查 backend 的数据库迁移、PostgreSQL 及索引启动日志 |
| 本机入口正常，公布地址超时 | 修正示例/过期地址；核对局域网防火墙、外部端口映射、NAT loopback 或 DNS |
| 本机正常，访问者机器失败 | 从访问者所在网络检查真实地址、监听范围和路径；本机检查不能代替外部验证 |

若只在局域网访问，使用真实局域网 IP 和本机监听端口，无需映射外部端口。
修正输入配置后重新生成并应用前端、Nginx 和网关配置：

```bash
read -r -p 'Actual Explorer URL (including port): ' explorer_url
usdb-explorer configure --local-node --explorer-url "$explorer_url" --http-port 28080
usdb-explorer down
usdb-explorer prepare --replace
usdb-explorer up
usdb-explorer check
```

新版本也可用 `check --url http://127.0.0.1:28080` 做临时定位；它不修正公布地址。
不要通过延长超时、切换 mining 或重建数据库修复错误 URL。

**恢复标志**：正常的 `check` 通过，访问者机器能加载页面及 API；空链返回 genesis/空列表即可。

## 页面打开但数据不显示

HTML 和数据经过不同路由。前端使用 prepare 时的 `explorer_url` 生成 API origin；只修改路由器、
手工访问另一 IP 或只改源 JSON，都不会改变已运行前端的 API 地址。
核对浏览器开发者工具中的失败请求目标，修正源配置并完成 `down → prepare --replace → up`。

后端刚启动可能尚在初始化。查看 `usdb-explorer logs --follow`，区分数据库 migration、RPC 错误
和正常等待首个区块。某些供应量/奖励统计接口当前有意未开放，见[使用范围](using-explorer.md#公共-rpc-与功能范围)。
不要把 `/api/` 绕过网关直连 backend，以免丢失 USDB 展示限制。

## 旧采样配置

旧模板曾固定 `rpc.historical_block=35` 和某笔测试交易。若当前高度为 0，采样 35 必然失败；
`prepare --replace` 会保留源配置中的固定目标。
local-node 模式使用 `configure --local-node --auto-samples` 备份并清除这两个字段，再按部署替换流程应用。
external 模式在源配置中移除这两个字段，保持原 RPC 地址，然后重新 prepare。
显式指定的业务验证样本不要随意删除，应先核对目标网络和 canonical 状态。

## RPC 分类与恢复

| 错误 | 含义与处理 |
| --- | --- |
| `HISTORICAL_STATE_UNAVAILABLE` | 历史状态已裁剪或不完整；archive 开关不恢复旧历史，保留原目录，安排完整归档恢复或独立重放 |
| `TRACING_UNAVAILABLE` | 上游未开放方法或私有代理拦截；确认 query-mode、配套镜像和两个 `debug_trace*` 方法；保持 debug 私有 |
| `TRACER_UNSUPPORTED` | 节点缺少 callTracer；核对 chain 镜像兼容性 |
| `RPC_DNS` / `RPC_CONNECTION_REFUSED` / `RPC_CONNECTION` | 按报错路由检查解析、监听和网络连通 |
| `RPC_TIMEOUT` / `TRACING_TIMEOUT` | 区分错误地址、网络超时和执行负载；超时不能直接证明缺少 archive，也不能由未 mining 解释 |
| `RPC_AUTH` / `RPC_TLS` | 检查受控代理访问规则、证书和 CA，不跳过 TLS 校验 |
| `RPC_RATE_LIMIT` | 检查并发和请求频率，降低负载后重试 |
| `RPC_HTTP` / `RPC_INVALID_RESPONSE` | 核对 HTTP 状态、端点路径和后端响应格式 |
| `SAMPLE_ABOVE_HEAD` | 固定高度超出观察链头；等待目标区块或显式恢复自动采样 |

## 部署文件、资源与数据库问题

`file changed` 表示 prepare 生成的文件被手工改动；从源配置重新 prepare，不改摘要绕过检查。
数据库身份/凭据不匹配时，恢复原部署关系和凭据，不删除数据库卷或用随机新密码接管。
已有卷的新主机恢复流程见[部署参考](../../explorer/README.md#6-独立升级停止与恢复)。

内存预算超限时核对本机全部容器限额、非 Docker 服务和系统余量。默认 `auto` 只计入已运行的
其他容器，不能替节点预留未来 Explorer 的预算。`local-node` 不支持远程 Docker、rootless 或 Docker Desktop；
这些环境应改用可达的 external 私网 RPC，而不是把容器里的 loopback 当作宿主机。

下载 404 时确认 release 已发布和附件名正确；安装校验失败时保留现状，核对版本和来源，不跳过校验。

## 求助信息

提供安装版本、出错时间、命令、第一项错误分类、`status` 摘要，以及上述哪条访问链路失败。
数据问题补充公开区块/交易哈希；资源问题补充磁盘余量和内存预算。日志只截取对应时段并脱敏。
不提供完整配置、`credentials.json`、数据库密码、钱包秘密或私有访问参数。
