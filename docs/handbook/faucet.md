# 测试网水龙头

[返回手册首页](README.md)

水龙头是 Explorer 的可选服务，默认关闭。安装包含本功能的后续 release 后才能使用以下命令。
它把运营者已有的测试 USDB 转给申请者，不修改发行规则。多个运营者可以各自提供水龙头，
使用独立的账户和额度，不需要每台矿机都部署。

## 领取测试币

打开 **USDB → Testnet faucet**，输入普通 USDB 钱包地址，点击 **Request test USDB**。
无需登录、连接钱包或签名。首版只支持普通账户，不支持合约钱包；不提供 Bitcoin，不能支付
Bitcoin 侧的矿工证铭刻费用。

页面显示发放金额、地址冷却、每日预算、确认数，以及排队／等待出块／等待确认／已到账状态。
节点尚未出块时申请会继续等待。交易链接可以打开浏览器的交易详情。

提交前页面保存申请编号。连接中断后使用 **Retry same application**，同一标签页刷新也会
恢复申请。**Use another address** 不会取消已经排队的转账。重复点击和网络重试受服务端的
持久化额度与防重复规则约束。

## 启用和额度

以下金额只是配置示例，应按账户余额和测试操作所需费用调整：

```bash
usdb-explorer faucet configure --enable --claim-amount 1 --daily-budget 100
usdb-explorer down
usdb-explorer prepare --replace
usdb-explorer up
usdb-explorer faucet status
```

首次部署尚未准备时使用 `prepare`，不加 `--replace`，也无需先 `down`。
`faucet configure` 修改配置源并保留备份，不改变运行中的部署；支持 `--config`。
`status`、`fund` 支持 `--state-dir`，用于选择不同的已准备部署。

服务首次启动自动生成专用账户。`faucet status` 显示地址、余额（整数 atoms，1 USDB =
10^18 atoms）、状态、预算占用，以及最近 20 笔补款的恢复编号与最后记录的状态。
余额不足时暂停新申请，补款后自动恢复；补款的最新回执用 `fund --retry` 查询。

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `--claim-amount` | 启用时必须指定 | 固定发放的 USDB |
| `--daily-budget` | 启用时必须指定 | 本实例 UTC 每日总预算，包含最大手续费预留 |
| `--cooldown-seconds` | 86400 | 地址两次申请的最小间隔，从接受申请时开始 |
| `--ip-daily-claims` | 10 | 同一出口 IP 滚动 24 小时最多接受的申请数 |
| `--ip-requests-per-minute` | 10 | 同一 IP 每分钟提交次数，包含失败和重复请求 |
| `--gas-reserve` | 0.01 | 发放账户保留的 USDB；矿工补款也保留同样的余额 |
| `--max-gas-price-gwei` | 100 | 允许的最高 gas price，超过则等待 |
| `--confirmations` | 3 | 转账在当前规范链达到此深度才记为到账 |

金额使用十进制字符串，不接受科学计数法。每笔申请预占 `claim_amount + 21000 × max_gas_price`，
因此示例预算 100 USDB 不保证每天恰好发 100 笔。重启不清零额度；未结束申请的预留跨日保留，
在结算日计入预算。失败或不支持的收款账户也不会自动退回当天预算、地址冷却。
每个实例最多有 100 笔活动领取申请。

所有额度都是本实例范围，不是全网身份限额。IPv6 按 /64 聚合，共享 NAT 出口可能共用 IP 额度。
IP 原文不写入账本，用与本钱包绑定的摘要计数。验证码、跨实例额度和邀请系统暂列 TODO。

## 从矿工账户补款

水龙头账户由服务自己管理；补款命令临时使用矿工私钥，把指定金额转入本部署的水龙头账户。
不需要向 USDB-chain 导入账户或开放账户解锁 RPC。

```bash
usdb-explorer faucet fund --amount 100
```

终端提示隐藏输入矿工私钥。私钥通过标准输入传给容器内的一次性命令，不写入配置、日志、
Docker 环境变量或账本；后台发放进程不保存它。命令显示补款恢复编号和交易哈希。
显示 `pending` 只表示已广播，到账仍需等待出块与确认。

也可以指定便于识别的编号，从环境变量读取私钥：

```bash
read -r -s -p 'Miner private key: ' USDB_MINER_PRIVATE_KEY
echo
export USDB_MINER_PRIVATE_KEY
usdb-explorer faucet fund --amount 100 --request-id refill-20260919-01 \
  --miner-key-env USDB_MINER_PRIVATE_KEY
unset USDB_MINER_PRIVATE_KEY
```

命令从自身环境移除所选变量后再执行 Docker；父 shell 仍由最后的 `unset` 清除。
同一编号只代表一笔补款，不能更换金额或矿工账户后重用。补款期间避免从同一矿工账户同时
发起其他交易；已有待处理交易时命令会暂停，不抢占其交易序号。

**超时、连接断开或确认到账时使用原编号重试，不创建新的补款编号：**

```bash
usdb-explorer faucet fund --retry --request-id refill-20260919-01
```

重试查询回执或重发已保存的同一笔签名交易，无需再次提供矿工私钥。原命令在保存交易前失败
时会查不到记录（`FUNDING_NOT_FOUND`），可使用相同编号、金额和私钥重执行原命令。
丢失编号时先查看 `faucet status` 中的 `funding` 列表。

首版补款由命令明确触发，没有后台自动划款调度。也可以用普通钱包直接向水龙头地址转账。

## 部署、升级和备份

水龙头使用 gateway 发布镜像中的独立 `/faucet` 程序，以非 root 身份运行。
只有该容器挂载 `${deployment_id}_faucet-data` 卷，包含 `wallet.key`、SQLite 账本与恢复记录。
钱包私钥是 0600 文件。不要将同一发放账户交给多个实例同时使用。

`down`、升级、`prepare --replace`、关闭再启用功能都保留该卷。
**准备目录备份不包含 Docker 卷。** 停止本部署后，用运维已有的 Docker 卷备份工具单独备份
完整水龙头卷，钱包与账本必须一起保存。项目名与卷名可从 `compose.json` 核对。
恢复时还要恢复匹配的网络配置，并核对原账户地址后再开放领取。
过期备份可能缺少后续出款记录；账户交易序号与账本不符时服务会暂停，需要先对照链上记录核查。

不要删除卷、单独清空账本或仅复制私钥重建服务，这会丢失资金访问能力或防重复记录。
账本存在但私钥丢失时服务拒绝生成新钱包；网络或钱包与账本不匹配也拒绝启动。

内置 nginx 自动增加 `/api/faucet/v1/` 路由，不增加对外端口。
外部 nginx 模式需应用重新生成的 `nginx.locations.conf`；水龙头仅发布本机
`127.0.0.1:28083`，可通过配置源的 `ingress.faucet_port` 调整，不应做公网映射。
路由片段含私有代理令牌，不能公开上传；nginx 会覆盖访客提交的客户端 IP 头。
若前面还有反向代理，只对明确受信任的代理配置真实 IP 解析，不直接信任访客的转发头。

关闭功能（钱包和账本继续保留）：

```bash
usdb-explorer faucet configure --disable
usdb-explorer down
usdb-explorer prepare --replace
usdb-explorer up
```

## 排错

| 状态／错误 | 处理 |
| --- | --- |
| `INSUFFICIENT_FUNDS` | 给水龙头补款；补款命令报错时检查矿工余额，保留手续费和 gas reserve |
| `DAILY_BUDGET_EXHAUSTED` | 等待新 UTC 日及旧申请结束，或调整预算后重新准备部署 |
| `ADDRESS_COOLDOWN` / `IP_DAILY_LIMIT` | 等待冷却／滚动窗口，换申请编号不能绕过额度 |
| `GAS_PRICE_TOO_HIGH` | 等待费用下降，或明确调整上限；已签交易不自动加价 |
| `RPC_UNAVAILABLE` / `WRONG_NETWORK` | 检查私有 RPC 连通性和网络身份，不改账本绕过 |
| `NODE_SYNCING` | 等待节点同步 |
| `BROADCAST_UNCERTAIN` | 结果不明；领取后台重试原交易，补款用原编号 `--retry` |
| `SENDER_HAS_PENDING_TRANSACTION` | 等待已有交易，避免多个程序同时使用同一账户 |
| `FUNDING_PENDING_USE_RETRY` | 用 `faucet status` 找到未结束的补款编号并重试 |
| `NONCE_CONFLICT` / `NONCE_OR_STORAGE_CONFLICT` | 核查外部转账或深重组，保留账本和哈希，不清空重试 |
| `WALLET_MISSING_RESTORE_BACKUP` / `STORAGE_IDENTITY_OR_IO_ERROR` | 核查卷权限、磁盘和网络，恢复匹配的钱包与账本 |
| 长时间 `pending` | 检查出块、交易池和费用；自动加价替换尚未实现 |

后台在签名／广播前核对 read 与 broadcast 上游的 chain ID 和创世块；使用 EIP-155 普通转账，
广播前保存完整签名交易。回执必须属于当前规范链并达到确认数；确认深度不是绝对最终性，
深重组或外部账户操作导致的交易序号冲突需要人工核对。

本地测试使用模拟链，覆盖并发、持久额度、错误网络、超时、重组，以及真实容器／nginx 的
账户持久化和页面流程。开放前仍应在目标测试网用专用小额账户完成补款、领取、到账和重启验收。
验证码、自动补款调度、自动加价和跨实例额度暂列 TODO。
