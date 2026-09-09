# 发布与迁移

## 首次仓库切换

1. `buckyos/usdb-explorer` 远端仓库使用 `main`。本地迁移提交接在远端初始化提交之后，再推送到 `main`。
2. 配置独立的 `usdb-explorer-release` environment、所需 reviewer/ref 规则，以及 Actions 的
   contents/packages 权限。构建器推送 `ghcr.io/buckyos/usdb-explorer-gateway`，公开安装还要求
   该 package 可供目标操作者拉取；新镜像 package 的可见性需在首次构建后核对。
3. 等待新仓库 CI 通过，再创建首个独立 tag（建议 `v0.2.0`），等待 build 草稿完成并运行 Publish。
4. USDB 原仓库提交迁移删除与文档；Go 仓库提交旧 public tag 入口的退役提示。
   首次切换不更新或移动任何旧 release tag。

在原仓库移除当前 public 源码前，完整副本已放入本地新仓库。迁移来源 commit/路径有记录；
首次提交采用快照导入，原始逐文件历史仍可在 USDB 原 commit 中追溯。

## 正常发版

`ci.yml` 执行独立单元/部署/安装/迁移/Nginx 检查。`release-build.yml` 只响应本仓库 `v*` tag，
检查 annotated tag，重新运行 CI，再生成 gateway 镜像和四个 release 资产：

- `usdb-explorer-vX.Y.Z.tar.gz` 及 `.sha256`
- `install-usdb-explorer-vX.Y.Z.sh` 及 `.sha256`

安装脚本绑定本仓库 `/releases/download/vX.Y.Z/` URL 与 archive digest。
Publish 从 `main` 固定本次 publisher commit，输入目标 tag，校验原 build、源码、镜像 lock、
附件摘要及安装脚本，经独立 environment 后重验并发布为 Pre-release。四个匿名下载逐一核对。
已发布版本重跑只校验；代码或附件内容变化须使用新 tag。

## 旧版兼容

旧 `usdb-public-v0.1.0` 附件和安装 URL 留在 `buckyos/usdb`。
原仓库的 legacy Publish allowlist 仅含该版本，并从固定迁移前 commit 运行其原始 publisher。
旧 build workflow 保留为迁移提示，以保留历史 workflow 身份供原 publisher 查询；它不响应新 tag push。

新安装器兼容旧安装根目录和 `current` 指针，新增 `usdb-explorer` 命令，并保留 `usdb-public`。
首次迁移不改配置和 deployment schema，不移动目录、复制数据库或修改凭据。
实际应用新部署镜像仍按备份 → down → prepare --replace → up 执行，并保持 deployment ID。

`tests/test_migration.py` 使用原发布的真实 v0.1.0 安装脚本/压缩包，离线安装后升级到新版本，
检查配置、凭据、Compose project、volume identity 和两个命令。测试使用临时目录及 fake Docker，
不管理真实部署。
