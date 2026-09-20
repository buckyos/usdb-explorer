# 交互式配置

[返回手册首页](README.md)

安装包含本功能的 Explorer release 后，用原安装账号执行：

```bash
usdb-explorer setup
```

首次安装和后续修改配置使用同一个命令。交互方式与 `usdb-node setup` 一致：回车接受当前值，
逐项选择后查看摘要，确认保存。Explorer 支持重复运行；已有源配置优先作为默认值。
源配置缺失、但指定目录已有完整部署时，会先校验部署摘要，再读取其中的配置恢复为默认值。

安装器会显示 `setup` 命令，不会自动启动向导。向导要求交互终端，脚本仍可使用
`configure --local-node`、`faucet configure`，或管理源 JSON。旧版本没有本命令时先升级工具。

## 向导中的选择

| 提示 | 选择与含义 |
| --- | --- |
| Node connection | `local-node` 连接同机回环 RPC；`external` 连接主机和容器均可达的私网上游 |
| Change private RPC endpoints | 默认 `n` 保留当前地址；新切换到 external 时必须填写 read RPC，可分别配置 tracing、broadcast 和可选 BTC indexer |
| Ingress | `bundled` 使用内置 Nginx；`external` 使用管理员维护的外部代理 |
| Visitor URL | 用户浏览器真实使用的完整 origin，含协议和必要的外部端口，不能有子路径 |
| Nginx host bind address | 本机监听地址；新选择回环 URL 时默认 `127.0.0.1`，非回环 URL 默认 `0.0.0.0` |
| Also publish Nginx over IPv6 | bundled 模式可选；未配置时默认 `n`，已有配置保留当前值；选 `y` 后保留 IPv4 并增加 IPv6 入口 |
| Nginx IPv6 bind address | 输入不带方括号的 IPv6 地址；public 默认 `::`，private 只能使用 `::1`；HTTP 和 HTTPS 共用监听范围 |
| Nginx local HTTP / HTTPS port | 本机端口，默认 `28080` / `28443`，可以与公网映射端口不同 |
| Certificate directory | bundled + HTTPS 时填写已有证书目录，包含 `fullchain.pem`、`privkey.pem` |
| Local frontend / gateway port | external 模式的回环后端端口，默认 `28080` / `28081`；启用水龙头时还可配置其回环端口，默认 `28083` |
| Enable testnet faucet | 新安装默认 `n`；已有配置保留当前开关。选 `y` 后填写单次发放与每日预算，可继续调整冷却、IP 限制、确认数和手续费策略 |
| Save this Explorer configuration | 查看摘要后保存；选择 `n` 或在交互期间按 Ctrl-C，不写入配置 |

同机默认 read RPC 是 `http://127.0.0.1:8545`，BTC indexer 是 `http://127.0.0.1:28020`。
向导只设置 Explorer 的连接方式，不会替节点开启 archive/tracing。节点准备和历史状态限制见
[安装前提](installation.md)。已有 `reference_url`、采样高度、交易样本、部署身份和自定义资源
预算保留；首次切换到 local-node、共机资源预留原为 0 时会改为自动探测。

例如公网 `38080` 映射到主机 `28080`，Visitor URL 填实际公网地址的 `:38080`，
Nginx local HTTP port 填 `28080`。域名使用 HTTPS 时，bundled 模式继续询问 HTTPS 端口和
证书目录；external 模式由外部代理管理证书。DNS、端口映射、证书签发/续期和防火墙由管理员管理，
详细步骤见[部署模式与访问地址](networking.md)。

启用水龙头时，向导首次提供 `1 USDB/次`、`100 USDB/日` 作为可修改建议值，保存前完整展示
限额。全局每日预算按本实例 UTC 日期计，包含最大手续费预留；IP 领取限制按滚动 24 小时计。
向导不收集 miner 私钥，也不进行转账。专用水龙头账户在服务首次启动时生成，
启动后用 `usdb-explorer faucet status` 查看，再按[水龙头手册](faucet.md)执行 `faucet fund` 补款。

## 保存与生效

所有选项先经过与 `prepare` 相同的配置校验。无效输入会提示修正，跨字段冲突可重新查看设置。
保存会原子替换源文件并创建权限为 `0600` 的原文件备份；源文件在交互期间被其他程序修改时，
保存会拒绝覆盖，需重新运行向导。使用期间不要直接编辑已 prepare 目录的文件。

保存成功只更新源配置，运行中的服务暂不变。向导根据所选部署目录，打印完整的下一步命令：

```bash
# 首次部署
usdb-explorer prepare
usdb-explorer preflight
usdb-explorer up
usdb-explorer check
```

已有部署修改配置后，先 `usdb-explorer down`，然后 `usdb-explorer prepare --replace`，
再执行上述 preflight、up、check。凭据、数据库卷、水龙头钱包及账本保持原身份；禁用水龙头
也不删除其卷。external 模式需在 check 前安装新生成的私有 `nginx.locations.conf` 并重载外部代理。

如果使用非默认路径，每次都传入匹配的两个路径，向导打印的后续命令也会带上它们：

```bash
usdb-explorer setup --config /path/to/config.json --state-dir /path/to/deployment
```

`--config` 必须指向部署目录及发布包之外的运维源文件。向导不会修改已有部署的 ID 或链身份；
另一套部署需要独立的配置文件、deployment ID、状态目录和入口端口。
