# testnet-v1 网络重置部署

[返回手册首页](README.md) · [网络契约](../network-contract.md)

v0 到 v1 是整个测试网络重置。Explorer 当前只包含 `usdb-testnet-v1`，chain ID `202610030`
（`0xc13956e`）；旧区块索引、数据库、水龙头余额和领取账本不迁移，也不要求备份废弃的 v0 数据。
此流程只清理 Explorer，正在运行的 v1 节点和矿工继续使用原数据。

## 清理已废弃的 v0 Explorer

以下针对默认配置目录和 `usdb-testnet-explorer` 项目。先用 `usdb-explorer status` 核对目标；
自定义部署应使用自己的 `--config`、`--state-dir` 和项目名。确认该实例属于废弃的 v0 网络后执行：

```bash
usdb-explorer down
docker compose --project-directory "$HOME/.config/usdb-public/default" \
  -f "$HOME/.config/usdb-public/default/compose.json" down --volumes
rm -rf -- "$HOME/.config/usdb-public/default"
rm -- "$HOME/.config/usdb-public/config.json"
```

Compose 删除该项目的 Explorer 数据卷，包括索引和水龙头数据。只删除指定部署和源配置；
证书目录 `~/.config/usdb-public/tls` 可继续使用。不要删除节点目录或运行全局 Docker prune。
旧配置的历史 backup 目录不参与新部署，不需要恢复；无需新建备份或兼容迁移。

## 创建 v1 部署

安装包含 v1 契约的 Explorer release，按[安装与接入](installation.md)选择固定版本。
只升级工具会保留旧源配置，因此上一步的配置重置不能省略。重新运行：

```bash
usdb-explorer setup
usdb-explorer prepare
usdb-explorer preflight
usdb-explorer up
usdb-explorer check
```

`setup` 从 v1 默认配置开始；同机节点选择 `local-node`。需要官方 HTTPS 入口时，直接按
[HTTPS 指南](https.md)填写访问域名、端口和证书目录。运行工具的账号必须能访问证书目录；
证书和私钥需分别为 `fullchain.pem`、`privkey.pem`，私钥只授予所需账号读取权限。
水龙头默认关闭；重新启用后生成新账户，需要重新补充 v1 测试币。

这是首次部署，使用普通 `prepare`。`prepare --replace` 保留同链数据库身份，不能跨网络使用。
启动前核对 archive/private tracing、Explorer 内存预算和 Docker 数据盘可用空间；
开始索引后等待 `check` 通过，并从实际访问者网络验证 HTTPS 证书、页面和公共 RPC。
钱包应添加 v1 chain ID；旧 v0 网络设置不能用于 v1 交易。
