# 镜像安全与测试网发布

本工程沿用 USDB [4246c0a7 的测试网策略](https://github.com/buckyos/usdb/commit/4246c0a7aab8c21501bb8bfe1a040579949cf391)：
功能回归持续执行，测试网漏洞扫描记录证据，显式安全审查可以选择严格执行。

| 范围 | 行为 |
| --- | --- |
| 普通 main/PR CI | 功能、安装、迁移、gateway、Nginx 和策略回归；不扫描正在变化的公网镜像 |
| 当前 testnet-v0 release build | 七个固定镜像 digest 均扫描；High/Critical 为 report-only |
| 手工 Security Review | 相同镜像集合，可选 report-only 或 strict |
| 主网 | 尚不支持部署；策略层固定 strict，拒绝 report-only 请求 |
| 扫描/数据库下载错误、缺少目标或证据、digest/source/platform 不符、上传失败 | 所有模式均失败 |

每个版本扫描 gateway、Blockscout backend/frontend、PostgreSQL、Redis、Nginx，以及 Go 构建镜像。
gateway 必须来自本仓库的 immutable digest，并携带匹配源码 commit 的 OCI revision label；
第三方镜像绑定选定 Explorer commit 的 lock 和网络契约摘要，不冒充由该 commit 构建。
平台固定为 linux/amd64，与发布包的部署平台一致。gateway 的 Go 二进制必须出现在扫描目标中。

Trivy 固定 0.74.0，扫描全部严重级别和未修复项。SARIF 由同一次 JSON 扫描转换，避免两次
扫描使用不同数据库；用法依据 [Trivy convert](https://trivy.dev/docs/latest/references/configuration/cli/trivy_convert/)。
在应用漏洞门禁前上传 `explorer-security-<image>-<run>-<attempt>` artifact，保留 90 天：

- `scan-input.json`：固定镜像、平台、网络、源 commit、锁与契约摘要、执行模式。
- `trivy-image.json`：完整原始扫描结果。
- `trivy-image.sarif`：High/Critical 结果。
- `metadata.json`：摘要、分类、计数、执行模式与 workflow 身份。
- `SHA256SUMS`：上述文件的内容校验。

report-only 允许存在未解决漏洞，不会将这些漏洞标记为已接受，也不会颁发镜像安全验收。
Explorer 没有继承节点镜像的安全例外或审查指纹；不能因为拆仓而复制、重新计算或续期那些例外。
`qualified_for_public_exposure=false` 和历史验收记录保持真实。未来引入例外须单独审查其范围、理由和期限。

## 手工审查

构建产生 gateway digest 后，在 Actions 选择 **Explorer Security Review**，Use workflow from 选择
构建它的 tag 或 commit 对应分支，填写完整 digest。优先选择不可变的 release tag，例如：

```bash
gh workflow run release-security-review.yml --repo buckyos/usdb-explorer --ref v0.2.0 \
  -f gateway_image='ghcr.io/buckyos/usdb-explorer-gateway@sha256:<64位真实digest>' \
  -f enforcement=strict
```

其余镜像自动来自该 ref 的 lock。选择错误源码、可变 tag 镜像或其他仓库 gateway 都会失败。
该流程不创建/替换镜像、tag、release 附件或安全例外；它也不改变已发布版本原来的 build 结果。
如果自动扫描因临时下载/扫描错误失败，可仅重跑失败的扫描 jobs；不要重建或替换已有 release 资产。

## 部署行为

原配置 schema、路径、deployment ID、凭据和数据库 volume 保持兼容；默认 exposure 仍为 private。
testnet-v0 可以显式配置 `ingress.exposure=public`，使用临时 HTTP IP＋端口预览或 HTTPS；
prepare/up 会提示未完成镜像验收。external 模式的 frontend/gateway 端口始终绑定 loopback，
由操作者的反向代理提供入口。bundled 模式可使用公网监听地址；选择 HTTPS 时要求已有证书，
并将 HTTP 重定向到配置的 HTTPS origin。HTTP 例外只适用于支持的测试网，不改变主网策略。
数据库和 Blockscout backend 不发布宿主机端口，RPC 白名单、上游网络身份、历史状态/tracing、
资源限额和数据身份检查继续执行。真实 archive/reorg/合约验证/钱包验收状态不因本策略调整而改变。
