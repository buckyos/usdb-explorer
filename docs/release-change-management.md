# Explorer 发布变更记录

沿用 USDB/go-ethereum 的结构化 fragment、完整提交清单、兼容性证据和确定性生成设计。
工具位于本仓库 `scripts/release_notes.py`，独立运行，不依赖节点仓库或节点 release manifest。
fragment 校验与 trailer 分类实现参考 USDB `3344a4c`，本仓库的 scope 列表面向 Explorer。

## 开发与打 tag 前检查

行为变化在开发完成后新增或更新未发布的 fragment；字段和 trailer 规则见
[schema](../.release-notes/README.md)。一项行为可以跨多个提交，不按文件或提交数量拆条目。
现有历史缺少 trailer 的提交保留为 unclassified，不伪造覆盖率。

`prepare_release.py` 在创建 tag 前校验 fragment，读取最近的已发布版本，检查该 revision 到
当前 HEAD 的新增条目、已发布条目的不可变性及完整提交清单。schema 错误、重复 trailer、
混用 `none`、修改已发布 fragment 或非 ancestor 范围会失败；未分类提交只报告，由维护者 review。
预检需要读取 GitHub Releases 的权限；网络/API 错误不会被当作“没有上一版”。

## 构建时冻结

Build 选择在该构建启动时已经发布、版本号低于本版的最高 Explorer tag（含已发布 Pre-release），忽略 draft。
要求它是 annotated tag、本地与远端 tag object 一致，且 commit 是当前版本的 ancestor。
没有已发布版本时按首发处理，纳入当前全部 fragment 和 Git history，不假设已有兼容性证据。

所有内容从 tag 对应 Git objects 读取，不读取未提交的工作区文件。构建冻结前版 tag object、
前后 revision、fragment、完整 commit inventory、gateway digest、兼容性输入和覆盖率，生成：

- `release-changes.json`
- `release-changes.json.sha256`
- `release-changes.md`

这三个附件与原来的安装脚本、压缩包及各自 checksum 一起上传，共七个附件。
GitHub Release 正文合并变更、升级操作、兼容性证据、提交范围、安装命令和固定 revision 的文档链接。
不再使用固定的 `explorer/RELEASE_NOTES.md` 项目简介。

## 兼容性比较

四个布尔 flag 由 fragment 声明和独立源码输入比较共同决定，取更保守结果：

| 输入变化 | 自动影响 |
| --- | --- |
| 网络 bundle、chain/network ID、genesis block hash、BTC 网络/起点/registry | `network_reset` |
| RPC contract、配置 schema、deployment schema | `config_change` |
| 固定运行时镜像 reference、gateway 源码或 Dockerfile | `restart_required` |
| Go 构建镜像 reference（提供 gateway 二进制和运行时 CA 证书） | `restart_required` |

分类优先级为 network reset → data rebuild → config change → restart → in place，保留全部并列 flags。
首次发版没有前版可比较。自动比较不等于完整兼容性验收：外部数据库 layout、浏览器语义及实际升级
动作仍需人工 review 并在 fragment 中声明，不能仅凭文件未变宣称安全兼容。

## Publish 与重跑

Publish 根据 **tag 中的 policy** 决定是否必须存在七个附件，因此缺失变更记录会失败，不能降级。
它校验附件摘要和源码后，使用记录里已冻结的前版边界重新生成 JSON、Markdown 和 Release 正文，
要求逐字节一致。修改条目、正文或重新计算被改写 JSON 的 checksum 都不能绕过源码重验。
审批前后的 fingerprint 继续覆盖完整附件和正文，发布后匿名下载核验覆盖全部七个附件。
Publish 同时按原 build 的 `created_at` 核验前版选择，记录不能任意切换基准。
重跑不因构建启动后才发布的另一个版本而改变边界；前版被撤回或移动 tag 则停止发布并要求核查。

已有 `v0.2.0` 没有 policy，仍按原四附件流程处理，不修改其 tag、草稿或正文。
第一份结构化记录的实际前版由构建时已发布状态决定；不能预先把仍为 draft 的版本当作基准。

## 本地复核

```bash
python3 scripts/release_notes.py validate-fragments
PYTHONDONTWRITEBYTECODE=1 python3 tests/test_release_notes.py
PYTHONDONTWRITEBYTECODE=1 python3 tests/test_public_release_publish.py
```

已有本地 tag 可离线生成预览：`generate --previous-release none` 表示首发，或提供精确前版 tag；
还需 `--release-id`、`--gateway-image`（完整 digest）、`--output-dir` 和 `--notes` 路径。
正式 workflow 使用默认 `auto`，从 GitHub 已发布状态选择边界。输出路径已存在时工具拒绝覆盖。
