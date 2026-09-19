# 安装与接入

[返回手册首页](README.md) · [版本范围](README.md#版本与验证范围)

## 1. 准备主机和上游

Explorer 支持 Linux amd64、Python 3.11+、Docker Engine 25+ 和 Compose v2；同机 USDB 节点
有更高的 Docker/Compose 基线时，以节点要求为准。同机模式需要本机 rootful Linux Docker。
独立 Explorer 预览环境以至少 8 GiB 内存为起点，默认容器预算合计 6 GiB；还需计入上游和系统余量。
持续监控 PostgreSQL、backend-data 的空间增长，不能按节点快照大小推算 Explorer 容量。

完整浏览器需要上游 archive 和私有 tracing。新节点在 `usdb-node setup` 的
`Provide full Explorer support (archive + private tracing)` 选择 `y`；普通节点默认 `n`。
专用查询服务器可使用 `full` 角色，不必挖矿。首节点是否启用 mining 由节点运维流程决定。

已有节点需要修改时，在维护窗口按节点的
[查询模式指南](https://github.com/buckyos/usdb/blob/master/doc/publish/usdb-node-query-mode.md)执行：

```bash
usdb-node down
usdb-node set-query-mode --state-mode archive --tracing on
usdb-node query-mode
usdb-node up
```

此操作会中断节点服务，要求 node kit 和 chain 镜像配套支持；Explorer 升级不会代替它。
启用 archive 不会补回已裁剪的旧历史，应保留原数据，通过独立目录重放或完整归档备份恢复。
同机节点还需为 Explorer 预留资源，不能只看当前 RSS；详见节点查询模式指南的资源预算部分。

## 2. 安装工具

从 [Explorer Releases](https://github.com/buckyos/usdb-explorer/releases) 选择运维方确认的固定版本，
阅读升级说明，复制该版本正文中的一键安装命令。使用运行 Explorer 的普通账号，不用 root 另建一套部署。
不要使用 Source code 压缩包或尚未发布草稿的下载链接。

安装器校验下载和包内文件，只安装工具、切换版本入口并在首次安装时创建配置，不启动服务。
若找不到命令，在当前 shell 执行：

```bash
export PATH="$HOME/.local/bin:$PATH"
usdb-explorer --help
```

| 文件 | 默认位置 |
| --- | --- |
| 源配置 | `~/.config/usdb-public/config.json` |
| 已 prepare 的部署 | `~/.config/usdb-public/default` |
| 版本目录 | `~/.local/share/usdb-public/releases/` |
| 当前版本指针 | `~/.local/share/usdb-public/current` |

源配置变更后必须重新 prepare 才会进入部署。不要直接改部署目录中有摘要保护的 JSON 或 Nginx 文件。

## 3. 选择访问地址与 Nginx 模式

默认是 `local-node + bundled`：连接同机 `127.0.0.1:8545`，并运行内置 Nginx `proxy` 容器。
Nginx 没有从 Explorer 移除；它统一服务网页、`/api/`、`/rpc` 和 `/network.json`。

| 访问方式 | 公布的 explorer URL | 监听与转发 |
| --- | --- | --- |
| 仅本机 | 默认 `http://127.0.0.1:28080` | 默认仅回环监听 |
| 局域网 | 服务器实际局域网 IP + `:28080` | bundled 配置非回环 URL 后监听 `0.0.0.0:28080`；放行所需局域网来源 |
| 公网映射 | 实际公网 IP/域名 + 外部端口 | 外部 TCP 端口转发到宿主机 `28080`；工具不创建路由器/云平台规则 |
| 已有 Nginx | 运维管理的真实域名 | 显式选择 `external`，由管理员安装生成的 location 配置；详见部署参考 |

配置实际访问者使用的 URL。以下提示要求输入真实地址，不内置示例 IP：

```bash
read -r -p 'Actual Explorer URL (including port): ' explorer_url
usdb-explorer configure --local-node --explorer-url "$explorer_url" --http-port 28080
```

局域网使用时无需公网映射。公网外部端口可以不同于本机监听端口，例如外部 38080 转发到本机 28080，
此时 URL 填外部端口，`--http-port` 仍填 28080。浏览器中的 API 地址由公布 URL 生成，不能只改入口转发。
本机检查公布的公网 URL 还依赖 NAT loopback/分离 DNS；最终须从真实访问者所在网络检查。
只开放浏览器入口，不公开节点 `8545/8546` 或数据库端口。测试网可临时使用 HTTP；HTTPS/证书见
[部署参考](../../explorer/README.md#4-使用内置-nginxbundled-模式)。

## 4. prepare、预检和启动

```bash
usdb-explorer prepare
usdb-explorer preflight
usdb-explorer up
usdb-explorer status
usdb-explorer check
```

首节点只有 genesis 时可先启动，等待区块和交易；不要求先挖矿或连接其他 peer。
`PASSED WITH WARNINGS` 表示可启动，但真实交易验证仍未执行。开始产生交易后重新 `check`。
v0.2.4 中对应结果只显示 JSON `PREFLIGHT_READY_NO_TRANSACTION_SAMPLE`。
超时、tracing 方法未开放、身份不符不是空链的正常表现，按[故障排查](troubleshooting.md)处理。

已有部署需要应用新地址或其他配置时：

```bash
usdb-explorer down
usdb-explorer prepare --replace
usdb-explorer preflight
usdb-explorer up
usdb-explorer check
```

`down` 只停止 Explorer；`prepare --replace` 备份旧部署、保留凭据和数据库卷。安装包不会自动启动服务。
升级保留了旧模板的高度 35/固定交易时，可先执行 `usdb-explorer configure --local-node --auto-samples`，
再按上述替换顺序应用。不要删除数据库以解决采样配置问题。

独立机器使用 `rpc.mode=external` 和明确的私网 read/trace/broadcast 地址，三个地址需从该主机和
Explorer 容器均可达。不要为了连通而公开 debug；详见[完整 RPC 配置参考](../../explorer/README.md#1-安装与初始配置)。
