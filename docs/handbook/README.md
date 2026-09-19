# USDB Explorer 用户与运维手册

普通使用者从网络运维方取得浏览器地址即可使用，不需要安装节点或 Docker。
部署人员使用发布包中的 `usdb-explorer` 管理浏览器、公共 RPC 网关和可选内置 Nginx。

| 目标 | 阅读入口 |
| --- | --- |
| 查区块、交易、地址，理解空链和数据延迟 | [使用浏览器](using-explorer.md) |
| 安装 Explorer、连接同机节点、配置局域网或公网入口 | [安装与接入](installation.md) |
| 理解 local-node/bundled、外部 Nginx、端口映射、域名和 HTTPS | [部署模式与访问地址](networking.md) |
| 判断预检结果、检查服务、升级和备份 | [日常运维](operations.md) |
| 预检失败、check 超时、页面空白或数据不更新 | [故障排查](troubleshooting.md) |

## 版本与验证范围

本手册由 Explorer 仓库维护，跟随 Explorer `vX.Y.Z` 发版；节点的 `rN` 版本独立维护。
本次对照已发布 **v0.2.4**，并描述本分支尚未发布的检查输出改进：

| 功能 | 版本范围 |
| --- | --- |
| 同机部署、内置 Nginx、`configure --local-node` | v0.2.3 起 |
| 首节点 genesis 阶段启动、`--auto-samples` | v0.2.4 起 |
| 人类可读的 preflight/check 结果、`--json`、`check --url` | 本分支新增；安装包含此变更的后续 release 后使用 |

运行 `usdb-explorer preflight --help`、`usdb-explorer check --help` 核对已安装命令。
v0.2.4 默认输出 JSON，尚无后两种参数；旧版排错可使用本手册中的 curl 方法。
本次 v0.2.4 现场验证覆盖 genesis 上游、本机 HTTP 页面和区块/交易 API；不代表完成公网访问、
有交易链上的全历史回放、重组恢复、钱包广播或主网验收。

## 项目与文档边界

Explorer 负责浏览器、公共网关及其数据库。USDB 节点负责同步、挖矿、archive 和私有 tracing。
两者可在同一台服务器部署，但安装、升级、停止和版本选择是独立的。

- 节点安装和维护见 [USDB handbook](https://github.com/buckyos/usdb/blob/master/doc/handbook/README.md)。
- 上游查询模式见 [archive 与私有 tracing](https://github.com/buckyos/usdb/blob/master/doc/publish/usdb-node-query-mode.md)。
- 完整参数、外部 Nginx、TLS 和部署文件参考见 [部署参考](../../explorer/README.md)。
- 构建、tag 和 Publish 见 [发布与迁移](../release-and-migration.md)。

本目录是 Explorer 用户和运维操作的主要文档；USDB handbook 提供入口和节点侧准备说明，
不复制一套 Explorer 安装命令。查历史版本时使用对应 release tag 下的手册。

所有命令使用原安装账号执行。`usdb-public` 是兼容命令，默认目录仍使用 `usdb-public` 名称，
不要为了更名迁移或删除现有配置、凭据和数据库卷。
