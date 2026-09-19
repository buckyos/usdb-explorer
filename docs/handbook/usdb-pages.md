# USDB 网络概览与矿工证

[返回手册首页](README.md) · [使用浏览器](using-explorer.md) · [部署模式](networking.md)

本页对应本分支新增功能，需安装包含此变更的后续 release；已发布的 v0.2.4 不包含这些页面。
升级会替换 frontend 和 gateway，不要求重建 Explorer 数据库或重置 USDB 网络。

## 网络概览

左侧 **USDB → Network overview**（`/usdb`）分别显示：

| 指标 | 含义 |
| --- | --- |
| USDB chain head | 所连接 USDB 节点观察到的最新区块 |
| Explorer indexed height | Blockscout 已索引区块高度；与上述节点高度比较显示索引差距 |
| Miner Pass index | usdb-indexer 已提交的 BTC 高度及查询就绪状态 |
| Bitcoin stable target | balance-history 暴露给索引器的 BTC 稳定高度 |

USDB 高度和 BTC 高度属于两条链，不能互相比较。页面每 15 秒刷新一次；失败时保留上次成功数据，
明确标注不是新结果。这些指标描述当前 Explorer 的数据源，不证明全网已经同步。

下方提供 Chain ID、币种、公共 RPC、genesis 和最新区块哈希，可点击 **Add network to wallet**
请求钱包添加网络，也可手动复制参数。私有 read/trace/indexer URL 不会作为公开网络资料返回。
此操作不请求私钥、签名或转账。

## 矿工证浏览与查询

**USDB → Miner Passes**（`/usdb/passes`）默认按有效能量降序、矿工证 ID 升序展示活跃 Standard
候选矿工证，每页 25 条。该顺序用于审计查看，不预测下一位出块者，也不是全部历史矿工证列表。

- 使用完整铭文 ID（64 位交易哈希加 `i` 和铭文序号）查询矿工证；Collab 和非活跃矿工证也通过 ID 查询。
- BTC height 留空时查询索引器最新可用状态，填写时查询指定历史高度。历史数据不可用会明确报错。
- 详情包括状态、类型、所有者脚本哈希和可用的 BTC 地址映射、USDB 奖励地址、铸造高度、原始/协作/有效能量、等级、难度系数，以及声明的 Leader 和前序矿工证。
- 能量按十进制整数原值显示，避免浏览器浮点数精度损失；难度系数单位为 bps（10000 bps = 1）。
- 每份结果显示 BTC 高度，展开可查看 BTC 区块哈希、snapshot ID、system state ID 和 registry ID。
  翻页、详情和关联链接保持同一高度及状态标识。查询结果不会在后台自动跳到新高度。
- BTC 重组或历史状态变化时会提示重新查询；点击 **Latest active passes** 开始新的查询。
  “No active standard passes”表示成功返回空列表，与索引器未就绪或连接失败不同。

这些是索引器的只读状态视图，不证明某张矿工证已被某个 USDB 区块采用，也不等于奖励到账验收。
实际区块采用的矿工证、奖励和手续费入账见 [区块经济明细与验收](block-economics.md)，独立按区块核验。
全网供应量仍未完成验收；不提供水龙头、铸造或签名功能。
当前 USDB 测试网的矿工证仍关联 **Bitcoin mainnet**；测试 USDB 不能支付 BTC 铭文手续费。

## 接入索引器

`rpc.mode=local-node` 默认读取 `http://127.0.0.1:28020`，对应 usdb-node 默认 Docker 的私有索引器
端口。Explorer 通过已有私有转发通道访问，无需开放新公网端口。改过该端口时：

```bash
usdb-explorer configure --local-node --indexer-url http://127.0.0.1:28020
usdb-explorer down
usdb-explorer prepare --replace
usdb-explorer preflight
usdb-explorer up
```

`--indexer-url` 不修改 USDB 节点配置；同机模式只接受 HTTP loopback。源 JSON 中设置
`rpc.indexer_url: null` 可停用矿工证数据源；菜单仍存在并显示未配置状态。

`rpc.mode=external` 没有默认索引器地址。管理员需在源配置 `rpc` 中新增实际私网
`indexer_url`，确保 gateway 容器可达，再按上述 down/prepare/up 流程应用。
不要把索引器端口映射到公网。缺少这个可选数据源不会放宽或改变完整 Explorer 的 archive/tracing 预检要求。

索引器必须支持 `get_rpc_info`、`get_readiness`、UIP-0006 v1 的候选集与矿工证详情、历史状态绑定和
`get_pass_snapshot`。gateway 核对 BTC 网络与冻结的 activation registry；不兼容或不匹配时不展示矿工证数据。

## 排错

| API 错误码 | 含义与处理 |
| --- | --- |
| `INDEXER_NOT_CONFIGURED` | external 部署尚未配置 `rpc.indexer_url`，或已显式停用 |
| `INDEXER_NOT_READY` | 查询/状态尚未就绪；检查节点索引进度、历史回填或重组恢复，不要当作空列表 |
| `INDEXER_INCOMPATIBLE` | 上游缺少对应查询接口或版本；核对并升级节点，不能仅靠升级前端修复 |
| `INDEXER_NETWORK_MISMATCH` | BTC 网络或 registry 不一致；检查是否连错节点/数据目录，禁止用关闭校验绕过 |
| `PASS_NOT_FOUND` | 该 ID 在指定 BTC 高度不存在；核对 ID 和铸造高度 |
| `HISTORY_UNAVAILABLE` | 指定历史状态未就绪或未保留；检查索引状态，或重新查询最新状态 |
| `STATE_CHANGED` | 状态或分页游标失效；使用 Latest active passes 重新开始 |
| `UPSTREAM_UNAVAILABLE` | 私网数据源不可达/超时/返回失败；检查源配置及容器可达性 |
| `CHAIN_IDENTITY_UNAVAILABLE` | USDB RPC 不可达或 chain ID/genesis 无法确认；先处理节点连接 |
| `INVALID_UPSTREAM_RESPONSE` | 上游结果不符合固定查询契约；记录时间及查询 ID，管理员核对节点版本 |

公开健康观察可用 `curl -fsS "$explorer_url/api/usdb/v1/overview"` 和
`curl -fsS "$explorer_url/api/usdb/v1/passes"`。不要将带凭据的私网配置贴到公开问题中。
`preflight`/`check` 继续检查完整 EVM 浏览器能力；矿工证页的就绪状态通过上述专用接口确认。
