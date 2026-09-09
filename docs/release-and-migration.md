# 发布与迁移

本地拆仓与远端接续已经完成：Explorer `89e1cd2`、USDB `b8833d5`、go-ethereum `9066e2b47`。
Explorer 首次 [独立 CI](https://github.com/buckyos/usdb-explorer/actions/runs/34371479993) 已通过。
以下首发步骤仍按顺序核对；CI 通过不代表已有可下载的 release。

## 首次仓库切换

1. `buckyos/usdb-explorer` 远端仓库使用 `main`。本地迁移提交接在远端初始化提交之后，再推送到 `main`。
2. 独立的 `usdb-explorer-release` environment 已创建，只允许 `main` 分支使用；当前未配置
   required reviewer，Publish 由维护者手工 dispatch。Actions 已启用，workflow 分别声明
   contents/packages 权限。构建器推送 `ghcr.io/buckyos/usdb-explorer-gateway`，公开安装还要求
   该 package 可供目标操作者拉取；新镜像 package 的可见性需在首次构建后核对。
3. 等待新仓库 CI 通过，再创建首个独立 tag（建议 `v0.2.0`），等待 build 草稿完成并运行 Publish。
4. 原仓库迁移删除与旧 public tag 入口退役已提交。首次切换不更新或移动任何旧 release tag。

在原仓库移除当前 public 源码前，完整副本已放入本地新仓库。迁移来源 commit/路径有记录；
首次提交采用快照导入，原始逐文件历史仍可在 USDB 原 commit 中追溯。

## 正常发版

`ci.yml` 执行独立单元/部署/安装/迁移/Nginx 检查。`release-build.yml` 只响应本仓库 `v*` tag，
检查 annotated tag，重新运行 CI，再生成 gateway 镜像和四个 release 资产：

- `usdb-explorer-vX.Y.Z.tar.gz` 及 `.sha256`
- `install-usdb-explorer-vX.Y.Z.sh` 及 `.sha256`

随后按完整镜像 lock 扫描七个固定 digest，测试网默认 report-only。High/Critical 漏洞保留为
未解决项；扫描、镜像身份、证据生成或上传错误会让整个 build 失败。草稿可能已经存在，
Publish 仍要求原 build 完整成功。手工批次审查与证据格式见 [安全策略](image-security.md)。

安装脚本绑定本仓库 `/releases/download/vX.Y.Z/` URL 与 archive digest。
Publish 从 `main` 固定本次 publisher commit，输入目标 tag，校验原 build、源码、镜像 lock、
附件摘要及安装脚本，经独立 environment 后重验并发布为 Pre-release。四个匿名下载逐一核对。
已发布版本重跑只校验；代码或附件内容变化须使用新 tag。

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
