# 日常运维

[返回手册首页](README.md) · [版本范围](README.md#版本与验证范围)

启用水龙头后还需备份独立的钱包与账本卷。额度、补款、恢复和关闭功能见
[测试网水龙头](faucet.md)；`faucet fund` 会发送转账，其余浏览器检查命令保持只读。

## 预检、容器状态与访问检查

| 命令 | 检查对象 | 不能据此得出的结论 |
| --- | --- | --- |
| `preflight` | 配置中的上游 RPC、网络身份、状态和 tracing | 不访问公布的浏览器 URL，不保证前端或端口转发正常 |
| `status` | 本部署的容器状态和配置入口 | running 不代表完成索引或能够对外访问 |
| `check` | 先做 preflight，再通过公布 URL 检查 `/rpc` 和浏览器 API | 不等于全历史、重组、钱包广播或全网同步验收 |

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

`check` 默认使用源配置经 prepare 后的 `ingress.explorer_url`。NAT 回环或公布地址错误可能导致
preflight 成功、check 失败。在支持 `--url` 的版本中，可临时检查 HTTP bundled 本机入口：

```bash
usdb-explorer check --url http://127.0.0.1:28080
```

若改过监听端口，使用对应端口。该参数不修改源配置、前端环境或已生成文件，仍使用原上游。
输出会明确提示公布地址没有被检查，JSON 的 `ingress_check` 为 `override_origin`。
正常检查该字段为 `configured_origin`，也只证明执行命令的这台主机可以访问；公网访问需从外部网络复核。
HTTPS 仍要求正确证书和主机名；此功能不跳过证书校验，也不自动跟随入口重定向。
v0.2.4 不支持 `--url`，可使用[本机 curl 检查](troubleshooting.md#preflight-成功但-check-超时)。
若外网页面已正常而本机回访公网地址超时，保留实际公网 URL，按
[公网回访排错](troubleshooting.md#外网正常但服务器上的-check-超时)检查 NAT/防火墙，
不要用本机成功覆盖公布地址检查的失败结论。

地址、模式和 HTTPS 的修改见[部署模式与访问地址](networking.md)；证书原路径续期可以
`reload-proxy`，但域名、监听端口或模式变更需要重新 prepare。

## 日常观察

```bash
usdb-explorer status
usdb-explorer check
usdb-explorer logs --follow
```

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
