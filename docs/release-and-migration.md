# 发布与迁移

本地拆仓与远端接续已经完成：Explorer `89e1cd2`、USDB `b8833d5`、go-ethereum `9066e2b47`。
Explorer 首次 [独立 CI](https://github.com/buckyos/usdb-explorer/actions/runs/34371479993) 已通过。
首个独立版本 `v0.2.0` 的 [tag build](https://github.com/buckyos/usdb-explorer/actions/runs/34382926874)
已成功生成草稿；仍须完成 Publish，草稿附件不可匿名下载。

## 首次仓库切换

1. `buckyos/usdb-explorer` 远端仓库使用 `main`。本地迁移提交接在远端初始化提交之后，再推送到 `main`。
2. 独立的 `usdb-explorer-release` environment 允许 `v*` tag，保留 `main` 分支规则用于旧流程过渡；当前未配置
   required reviewer，Publish 由维护者手工 dispatch。Actions 已启用，workflow 分别声明
   contents/packages 权限。构建器推送 `ghcr.io/buckyos/usdb-explorer-gateway`，公开安装还要求
   该 package 可供目标操作者拉取；新镜像 package 的可见性需在首次构建后核对。
3. 首个独立 tag `v0.2.0` 已完成 build；按下方过渡说明完成发布。后续 tag 须包含新的 Publish workflow。
4. 原仓库迁移删除与旧 public tag 入口退役已提交。首次切换不更新或移动任何旧 release tag。

在原仓库移除当前 public 源码前，完整副本已放入本地新仓库。迁移来源 commit/路径有记录；
首次提交采用快照导入，原始逐文件历史仍可在 USDB 原 commit 中追溯。

## 正常发版

`ci.yml` 执行独立单元/部署/安装/迁移/Nginx 检查。`release-build.yml` 只响应本仓库 `v*` tag，
检查 annotated tag，重新运行 CI，再生成 gateway 镜像和安装资产：

- `usdb-explorer-vX.Y.Z.tar.gz` 及 `.sha256`
- `install-usdb-explorer-vX.Y.Z.sh` 及 `.sha256`
- `release-changes.json`、`.json.sha256` 和 `release-changes.md`

新版本合计七个附件，变更记录与正文从固定 tag 和前一已发布版本生成；规则见
[发布变更管理](release-change-management.md)。已有四附件版本继续兼容。

随后按完整镜像 lock 扫描七个固定 digest，测试网默认 report-only。High/Critical 漏洞保留为
未解决项；扫描、镜像身份、证据生成或上传错误会让整个 build 失败。草稿可能已经存在，
Publish 仍要求原 build 完整成功。手工批次审查与证据格式见 [安全策略](image-security.md)。

安装脚本绑定本仓库 `/releases/download/vX.Y.Z/` URL 与 archive digest。
Publish 在 **Use workflow from** 直接选择待发布的 `vX.Y.Z` tag，无额外版本输入；CLI 使用
`gh workflow run release-publish.yml --repo buckyos/usdb-explorer --ref vX.Y.Z`。
workflow 拒绝分支 dispatch，并要求选中 tag、dispatch commit 和 checkout 一致，校验原 build、
源码、镜像 lock、附件摘要及安装脚本，经独立 environment 后重验并发布为 Pre-release。
Build 和 Publish 共用该 tag 的并发锁，全部附件匿名下载逐一核对。
已发布版本重跑只校验；代码或附件内容变化须使用新 tag。

## v0.2.0 Publish 过渡

`v0.2.0` 固定在 `82aeaaf`，其中旧 Publish workflow 要求从 `main` 执行并输入 `release_id`。
选择 tag 后失败是旧 workflow 的 ref 检查导致；`v0.2.0` 是正确 tag 名，`0.2.0` 不是。
修改 `main` 不会更新已有 tag 中的 workflow，重跑原失败任务也仍使用旧代码。

在修复推送到 `main` **之前**，已有草稿仍可按旧入口发布：

```bash
gh workflow run release-publish.yml --repo buckyos/usdb-explorer --ref main \
  -f release_id=v0.2.0
```

修复推送后，新发版使用包含修复的独立 tag（例如 `v0.2.1`），直接选择 tag 发布。
保留 `v0.2.0` 的 tag、草稿和原始附件，不移动 tag 或覆盖附件以修改历史 workflow。

## 旧版兼容

旧 `usdb-public-v0.1.0` tag、未发布草稿和附件留在 `buckyos/usdb` 作为历史记录，不再安排发布。
原仓库的 legacy build/Publish workflow 已退役，后续只在 Explorer 仓库发版。
草稿附件仍不可匿名下载，旧安装 URL 的 404 保持原状；不能将它作为可用安装入口。

新安装器兼容旧安装根目录和 `current` 指针，新增 `usdb-explorer` 命令，并保留 `usdb-public`。
首次迁移不改配置和 deployment schema，不移动目录、复制数据库或修改凭据。
实际应用新部署镜像仍按备份 → down → prepare --replace → up 执行，并保持 deployment ID。

`tests/test_migration.py` 使用旧草稿中的真实 v0.1.0 安装脚本/压缩包，离线安装后升级到新版本，
检查配置、凭据、Compose project、volume identity 和两个命令。测试使用临时目录及 fake Docker，
不管理真实部署。
