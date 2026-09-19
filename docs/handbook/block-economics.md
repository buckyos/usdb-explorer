# USDB 区块经济明细与验收

本功能需要同时升级到包含此变更的 USDB-chain 和 Explorer 版本，尚不属于已发布的 Explorer v0.2.4。
只升级 Explorer 时，旧节点会显示“需要升级 USDB-chain”，网络概览、矿工证和普通区块查询继续工作。

## 使用入口

左侧 **USDB → Block economics**，输入 USDB 区块高度或完整区块哈希。普通区块详情底部的
**Verify USDB block economics** 和网络概览的最新区块入口也能跳转；这些链接固定到区块哈希。
本页不自动切换到新块，点击 **Verify block** 重新核对。

成功时显示 **Verified for this block**，并提供：

- 实际采用的 Miner Pass、BTC 锚点高度、锚点复用年龄、snapshot 和 system state ID；矿工证链接保留历史高度及状态。
- 本块新增发行、矿工取得的新增发行、交易手续费总额、矿工手续费和 Dividend 手续费。
- 每笔交易的执行结果、退款后的 gas、实际费用和费用去向；失败的 EVM 调用同样可能支付手续费。
- 区块执行前后的 issued counter、状态根、回执根，以及该高度生效的规则版本。

金额以 18 位小数的 USDB 精确展示，鼠标悬停可见整数 atoms。接口中所有金额都是十进制字符串，
客户端不能用 JavaScript `Number` 转换后计算。分账逐笔取整后求和，不能对整个块的费用统一乘比例。

**Issued counter 包括 genesis 初始分配及协议新增发行，不是流通供应量。** 手续费分账只是已有
USDB 的转移。本页的协议入账也不是地址净余额变化；同一区块的普通转账、合约调用可能改变收款地址余额。

Genesis 明确显示不适用，不伪造矿工奖励。空块可正常核验，交易手续费为零；空块仍可能产生新增发行。

## 验收依据与范围

节点从父区块的留存状态，在内存中重新执行目标区块的交易，按每笔交易执行前的 Dividend 门槛状态
确定费用去向，再调用原有共识奖励结算逻辑。奖励解析使用区块 selector 固定的 BTC 历史上下文。
只有重放结果的 **state root、receipts root、logs bloom、gas used** 均与区块承诺一致，且新增发行
与结算阶段的矿工入账吻合，才返回金额。查询不导入区块、不提交状态、不改变挖矿或协议规则。

v1 支持 payload/BTC anchor/difficulty/reward/emission/price v1、fee split 和 collaboration v0/v1、
quote v0、aux pool v0。其他组合明确返回未支持，不套用当前比例。手续费拆分是否启用还取决于
Dividend 启用高度、代码身份和 bootstrap 状态，不只取决于 fee split 版本。

这是该节点对**指定区块**的重放核对，不代表独立网络审计、所有历史区块或所有未来策略都已验收。
接口返回前检查 USDB canonical hash；重组后的旧块不会继续作为主链明细返回。浏览器已显示的数据
只是加载时的结果，不提供实时重组推送。

原 Blockscout `/api/v2` 的以太坊奖励/burn/supply 字段仍被网关屏蔽，`preflight/check` 的
`reward_supply_qualification: not_run` 仍指整体经济验收；本页的逐块报告不把该状态改成全局通过。
全网供应量统计和实时推送分别保留未验收/未开放提示。

## 部署与排错

节点需要留存目标块的父状态，以及奖励结算需要的 BTC 历史数据。沿用完整 Explorer 的
archive + 私有 tracing 部署；本查询内部重放，不把 debug/tracing 方法直接开放到公网。
新 RPC `eth_getUSDBBlockEconomics` 位于节点已有的私有 `eth` 命名空间，无需新端口或修改网络配置。
Explorer 通过 `read_url` 访问它；前面的私有 RPC 代理若自定义了方法白名单，需要允许该方法。
公共 `/rpc` 不转发该方法，公开查询只走有边界检查及限流的 GET API。

安装两端新版本后，节点按正常升级步骤替换程序/镜像；Explorer 执行：

```bash
usdb-explorer down
usdb-explorer prepare --replace
usdb-explorer preflight
usdb-explorer up
```

保留原配置、凭据和数据库卷，无需网络重置或重建 Explorer 索引。启用 archive 不会恢复已经裁剪的历史。

| 返回码 | 处理 |
| --- | --- |
| `ECONOMICS_NODE_UPGRADE_REQUIRED` | 升级 read_url 后面的 USDB-chain；检查私有代理方法白名单 |
| `ECONOMICS_HISTORY_UNAVAILABLE` | 使用留有父状态的 archive 节点；缺失历史须通过验证过的备份或独立重放恢复 |
| `ECONOMICS_POLICY_UNSUPPORTED` | 本报告版本未验收该块的策略组合，等待相应支持，不显示推算值 |
| `ECONOMICS_VERIFICATION_FAILED` | 检查节点日志、历史 Miner Pass 查询及区块状态承诺；不要手工解禁未核验金额 |
| `BLOCK_NOT_CANONICAL` | 区块已离开主链，按高度重新查询；保留旧 hash 供排错 |
| `BLOCK_NOT_FOUND` | 核对高度/hash 与节点同步进度 |
| `ECONOMICS_TIMEOUT` / `BUSY` / `RATE_LIMITED` | 稍后重试，检查节点负载；不要持续并发刷新 |
| `ECONOMICS_REPLAY_LIMIT` | 超过交互式重放范围，需要运维离线核验，不代表区块无效 |

节点最多同时进行 2 个重放，缓存最近 16 个成功报告并在读取缓存时检查 canonical hash；
每块最多 2000 笔交易、6000 万实际 gas。EVM 重放使用 10 秒期限，外部历史 RPC 还受其自身超时约束；
网关现有响应头超时为 8 秒，较慢请求会显示超时。缓存不写入持久化目录，重启后重新核验。

## 保存逐块报告

公开接口：`GET /api/usdb/v1/blocks/<十进制高度或小写0x哈希>/economics`。
不接受 `latest`、`pending`、自定义 RPC URL 或额外 query 参数。用实际浏览器入口替换下面的 origin：

```bash
curl --fail-with-body --max-time 20 \
  'http://127.0.0.1:28080/api/usdb/v1/blocks/215/economics' \
  -o block-215-economics.json
```

HTTP 200 后仍应检查 `economics.status`：`verified` 才是普通块通过，`genesis_not_applicable` 表示创世块不适用。
保存报告时同时记录节点/Explorer 版本、查询时间及测试场景；报告内包含区块 hash、规则版本和精确 atoms。
错误响应不包含金额、私有地址或上游原始错误。抽样通过不能替代全历史验收。

本地自动回归覆盖真实 EVM 转账、EIP-1559 有效 gas price、失败调用、gas 退款、分账门槛和策略切换高度、发行结算、节点重启、重放承诺不匹配、
canonical 分支切换、旧节点、网关金额精度和浏览器错误态；浏览器测试使用隔离的模拟数据，不能替代上线后抽样。
