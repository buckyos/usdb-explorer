# 网络与 RPC 契约

普通构建从 `explorer/networks/` 读取两个已提交文件：网络 catalog 与
`usdb-explorer-network-contract:v1` 契约。契约记录 USDB 源仓库、精确 commit、catalog SHA-256、
`usdb-explorer-rpc:v1` 能力要求与当前已支持的展示语义。

当前固定 USDB `0b946e03c71f70e6dfe0bb1bc44d9e6b3ce803ec`，保持原有 chain ID/genesis。
RPC URL 属于操作者配置，不作为权威网络身份来源。

需要更新时，在对应 USDB checkout 运行其导出工具：

```bash
python3 tools/export_explorer_contract.py --revision <40-character-commit> \
  --output-dir /tmp/explorer-network-export
```

工具从指定 commit 导出并验证 canonical network bundle，忽略工作区未提交的网络修改。
审阅来源、身份和 RPC profile 后，将两个导出文件一起复制到本工程的 `explorer/networks/`，运行：

```bash
python3 explorer/package_release.py --check-network
python3 tests/test_network_contract.py
```

再提交契约更新并运行兼容性测试。日常 CI 不自动下载或跟随节点 master。

浏览器需要历史状态、区块/交易/回执/日志、callTracer 和交易广播能力。读取、tracing、广播
上游分别配置并检查相同网络身份与 canonical checkpoint。面向外部的 gateway 仍按自己的
方法白名单提供服务，内部 tracing 上游不直接暴露为公共 debug 接口。

extraData 保持原始值。奖励、供应量和手续费分配中尚未完成 USDB 验收的字段继续
`not_qualified`；真实 gas paid 等已实现行为由 gateway Go 测试覆盖。

节点集成验收应记录固定节点镜像 digest、network contract 摘要、链数据样本和测试结果。
本次拆仓的 mock、迁移和 Nginx 测试属于本地回归，不表示真实 archive/reorg/wallet 验收通过。
