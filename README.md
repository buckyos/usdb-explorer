# USDB Explorer

USDB 区块浏览器与公共 RPC 接入服务。包含 Blockscout 集成、USDB 展示适配、受限 RPC gateway、
可选 Nginx，以及独立安装、备份、升级与发版工具。通过配置的 RPC 上游访问 USDB 节点。

本工程从 `buckyos/usdb` 的 `0b946e03c71f70e6dfe0bb1bc44d9e6b3ce803ec` 迁出，来源路径记录在
[MIGRATION.json](MIGRATION.json)。新版本使用本仓库的 `vX.Y.Z`，不要求创建节点 `rN` 或更新节点 release lock。

## 使用

完整步骤见 [部署与升级手册](explorer/README.md)。源码入口：

```bash
explorer/usdb-explorer --help
explorer/usdb-explorer prepare --config /path/to/config.json --state-dir /path/to/deployment
```

`usdb-public` 保留为兼容命令。原有配置/部署 schema、默认 `~/.config/usdb-public`、
`~/.local/share/usdb-public`、deployment ID 和数据库 volume 身份继续使用。
历史 `usdb-public-v0.1.0` 附件保留在原 USDB 仓库。

## 工程边界

| 目录 | 内容 |
| --- | --- |
| `explorer/` | 部署控制、安装器、网络 catalog/contract、第三方镜像 lock |
| `gateway/` | 独立 Go module：公共 RPC 策略与浏览器 API 适配 |
| `scripts/prepare_release.py` | 本仓库的 annotated tag 准备工具 |
| `.github/workflows/` | 独立 CI、构建草稿、手动 Publish |
| `tests/` | 部署、安装、迁移、发布、网络契约和真实 Nginx 测试 |
| `docs/` | 网络接口、发布与迁移说明、历史验收记录 |

日常构建和测试不需要 USDB/go-ethereum/SourceDAO checkout。网络身份和 RPC 要求通过已提交的
[契约文件](explorer/networks/usdb-testnet-v0.contract.json)关联固定 USDB commit；刷新流程见
[网络契约](docs/network-contract.md)。

## 验证

Python 3.11+，Go 版本见 `gateway/go.mod`。完整 Python 验证需要本地 Docker，用于创建和清理
带随机名称的临时 Nginx 容器/网络：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_*.py'
cd gateway
go test -race ./...
```

## 发布

在干净且已推送的 `main` 上：

```bash
python3 scripts/prepare_release.py --version 0.2.0
python3 scripts/prepare_release.py --version 0.2.0 --create --push
gh workflow run release-publish.yml --repo buckyos/usdb-explorer --ref main \
  -f release_id=v0.2.0
```

先等待 tag build 成功，再手动 Publish。完整配置与切换顺序见
[发布与迁移](docs/release-and-migration.md)。普通开发 checkout 即使尚无 commit 也可生成明确标记
`source_dirty` 的本地测试包；发布只接受通过 tag 构建的干净源码。

第三方镜像仍沿用原有私有兼容性预览基线。本次迁移没有替代镜像安全、真实 archive、重组恢复、
合约验证和钱包广播验收；原始结果见 [历史验收记录](docs/usdb-public-testnet-services-plan.md)。
