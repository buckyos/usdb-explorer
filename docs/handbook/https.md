# 使用已有证书启用 HTTPS

[返回手册首页](README.md) · [部署模式与端口](networking.md) · [交互式 setup](setup.md)

内置 Nginx（`ingress.mode=bundled`）可以直接提供 HTTPS，无需再安装宿主机 Nginx。
本页以已有同机部署为例，将 `http://usdb-testnet.tbudr.top:28080/usdb` 迁移到
`https://usdb-testnet.tbudr.top/usdb`，使用已有的 `*.tbudr.top` 证书。
其他部署请替换域名、账号和服务器地址；命令使用原安装账号执行，例中为 `usdb`。

## 1. 确定入口与端口

HTTPS 不要求使用 443；当前默认本机端口就是 **28443**，HTTP 默认端口为 **28080**。
可以直接使用公网 **28443 → node1:28443**，Visitor URL 填
`https://usdb-testnet.tbudr.top:28443`，IPv6 同样放行 TCP 28443。
下面的 443 方案用于省略访问地址中的端口号，不改变软件默认值。

如果公网及 node1 的 TCP 443 可用，建议内置 Nginx 直接发布宿主机 **443**。
公网、局域网和 IPv6 可以使用同一个 HTTPS 端口，便于配置分离 DNS 和排查公网回访问题。
保留已有 HTTP 28080 入口，迁移后由 Nginx 返回 308，跳转到新的 HTTPS 地址并保留路径。

| 入口 | 路由器 IPv4 转发到 node1 | 用途 |
| --- | --- | --- |
| `https://usdb-testnet.tbudr.top`，TCP 443 | `192.168.1.119:443` | 新的网页、API 与公共 RPC 入口 |
| 原有 HTTP TCP 28080 | `192.168.1.119:28080` | 旧链接自动跳转到 HTTPS |
| HTTP TCP 80，可选 | `192.168.1.119:28080` | 不带端口的 HTTP 链接跳转到 HTTPS |

先检查宿主机监听及已有 Docker 端口发布，确认 443 没有分配给其他服务，再配置路由器和防火墙。
域名 A 记录继续指向公网入口。若已配置 AAAA，则 IPv6 入口也必须能访问 TCP 443：
在 setup 中启用 IPv6 监听，并放行路由器和主机的 IPv6 防火墙。IPv4 端口映射不会替 IPv6 转发端口。
工具不修改这些网络配置。

也可以沿用默认本机 HTTPS 端口 **28443**，让公网 **443 → node1:28443**，Visitor URL 仍不带端口。
但 IPv6 直连和内网分离 DNS 的客户端会访问 URL 中的 443，需要另外提供该端口的入口；
只修改 AAAA 或内网 DNS 不能把 443 变为 28443。

如果公网 443 不可用，可使用公网 **28443 → node1:28443**，并将 Visitor URL 填为
`https://usdb-testnet.tbudr.top:28443`。证书校验匹配域名，不要求固定端口。
HTTP 与 HTTPS 本机端口必须不同；不能在原 HTTP 28080 监听上仅把浏览器地址改成 `https://`。
已有其他网站占用 443 时，也可考虑[接入现有 Nginx](networking.md#现有-nginx-提供-https)。

## 2. 安装证书与私钥

`*.tbudr.top` 覆盖 `usdb-testnet.tbudr.top` 这一层子域名；它本身不覆盖根域名 `tbudr.top`、
更深层的子域名或 IP 地址。证书须在有效期内，且签发 CA 受浏览器和钱包信任。
准备 PEM 格式的两个文件：

| 固定文件名 | 内容 |
| --- | --- |
| `fullchain.pem` | 域名证书在前，其后为签发方提供的中间证书链；不能只放中间证书 |
| `privkey.pem` | 与该域名证书匹配的私钥，供 Nginx 无人值守启动时读取 |

通配符范围、证书链顺序及私钥权限见 [Nginx HTTPS 文档](https://nginx.org/en/docs/http/configuring_https_servers.html)。
使用证书供应方提供的 Nginx/PEM 下载包；PFX/P12 容器文件不能仅改名后使用。

如果运维提供的是 `fullchain.cer` 和 `tbudr.top.key`，确认内容为 PEM 后，分别复制为
`fullchain.pem` 和 `privkey.pem` 即可，无需格式转换。完整证书链已经包含中间证书时，
不要再次拼接 `ca.cer`；`.csr` 和 `.conf` 文件不需要复制到 Explorer 的证书目录。

将下列 `/path/to/` 替换为证书实际来源路径。在首次安装时执行：

```bash
install -d -m 700 /home/usdb/.config/usdb-public/tls
install -m 644 /path/to/fullchain.pem /home/usdb/.config/usdb-public/tls/fullchain.pem
install -m 600 /path/to/privkey.pem /home/usdb/.config/usdb-public/tls/privkey.pem
```

目录和文件由原安装账号管理，私钥不需要提供给前端或钱包。内置代理将整个目录只读挂载，
Nginx 主进程需要能读取私钥。若来源文件只有 root 可读，由管理员复制并设置目标文件的所有者，
不要为了复制证书而以 root 运行整套 Explorer setup。当前向导没有私钥解密口令配置项。

文件必须实际位于此目录内，不能是指向目录外的符号链接。Certbot `live/` 下的文件通常是
指向 `archive/` 的链接，应复制实际文件到上述专用目录。不要把证书放到发布包或 `default/`
生成目录里，也不要移动已有的配置、凭据或数据卷。

检查域名和有效期，以下命令不会输出私钥：

```bash
openssl x509 -in /home/usdb/.config/usdb-public/tls/fullchain.pem \
  -noout -subject -issuer -dates -ext subjectAltName
openssl x509 -in /home/usdb/.config/usdb-public/tls/fullchain.pem \
  -noout -checkhost usdb-testnet.tbudr.top
```

这只是检查证书信息；完整证书链是否受客户端信任、入口是否加载正确证书，还需在启动后验证。

## 3. 用 setup 修改现有部署

```bash
usdb-explorer setup
```

按本页推荐的 443 方案填写；其他现有设置回车保留：

| 提示 | 当前同机部署的填写值 |
| --- | --- |
| Node connection | 保留 `local-node` |
| Change private RPC endpoints | `n`，保留当前上游地址 |
| Ingress | `bundled` |
| Visitor URL | `https://usdb-testnet.tbudr.top`，**不带 `/usdb` 或 `/rpc`** |
| Nginx host bind address | `0.0.0.0` |
| Also publish Nginx over IPv6 | 需要 IPv6 且主机和 Docker 已支持时选 `y`；已有双栈部署保留当前值 |
| Nginx IPv6 bind address | public 部署通常为 `::`，也可保留已配置的具体地址 |
| Nginx local HTTP port | `28080` |
| Nginx local HTTPS port | `443`，若选择默认端口映射方案则为 `28443` |
| Certificate directory | `/home/usdb/.config/usdb-public/tls` |
| Enable testnet faucet 及后续额度项 | 保留当前开关和额度 |
| Save this Explorer configuration | 核对摘要后选 `y` |

setup 只保存源配置并备份旧配置，不会立即改变运行中的服务。HTTPS URL 还会用于生成前端 API
地址和钱包网络元数据；不能只手改生成的 Nginx 文件。`configure` 快捷命令目前没有证书目录
或 HTTPS 端口选项，首次启用 HTTPS 使用 setup。

## 4. 应用配置并验证

安排短暂的 Explorer 中断，逐条执行；某一步失败时先处理错误，不继续后续步骤：

```bash
usdb-explorer down
usdb-explorer prepare --replace
usdb-explorer preflight
usdb-explorer up
usdb-explorer check
```

此流程保留数据库卷、凭据、水龙头钱包及账本，不重启 USDB 节点，也不重新同步区块。
首次部署使用普通 `prepare`；非默认部署继续使用原 `--config` 和 `--state-dir`，见
[setup 路径说明](setup.md#保存与生效)。不要删除卷或生成目录来应用 HTTPS。

`preflight` 检查上游节点能力，**通过不等于 HTTPS 已就绪**。`up` 会先检查内置 Nginx 配置；
随后 `check` 对比上游、本机、LAN 和公布入口。HTTPS 的本机检查保留域名 Host/SNI 和证书校验。
旧版工具若没有多入口检查，可在 node1 用下面的命令直接验证本机 TLS，同时避开公网回访：

```bash
curl --noproxy '*' --connect-timeout 5 --max-time 15 -fsS \
  --connect-to usdb-testnet.tbudr.top:443:127.0.0.1:443 \
  https://usdb-testnet.tbudr.top/network.json
```

若本机 HTTPS 端口选了 28443，只将 `--connect-to` 最后的 `:443` 改成 `:28443`；
URL 仍用公布的域名和外部端口。若公布 URL 也使用非标准端口，则同步修改 URL 和
`--connect-to` 的第一个端口。不要用 `https://127.0.0.1` 或 `curl -k` 代替域名证书校验。

再从实际访问者的外网电脑验证：

```bash
curl --noproxy '*' --connect-timeout 5 --max-time 15 -I \
  http://usdb-testnet.tbudr.top:28080/usdb
curl --noproxy '*' --connect-timeout 5 --max-time 15 -fsS \
  https://usdb-testnet.tbudr.top/network.json
curl --noproxy '*' --connect-timeout 5 --max-time 15 -fsS \
  -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","id":1,"method":"eth_chainId","params":[]}' \
  https://usdb-testnet.tbudr.top/rpc
```

第一条应返回 `308` 和 `Location: https://usdb-testnet.tbudr.top/usdb`；网络元数据中的
`rpcUrls` 应为 `https://usdb-testnet.tbudr.top/rpc`，RPC 应返回链 ID。
双栈域名可分别给 HTTPS curl 命令加 `-4` 和 `-6`，在具备对应网络的外部电脑上验证。
最后打开 HTTPS 页面并强制刷新，检查概览、区块、交易及钱包网络信息；已有钱包若仍保存旧 HTTP
RPC，需在钱包网络设置中更新地址，不能依赖 HTTP 重定向来修复钱包配置。

若本机通过、只有 `configured` 入口超时，而外网浏览器正常，按
[公网回访排错](troubleshooting.md#外网正常但服务器上的-check-超时)处理 NAT 回环或分离 DNS。
不要为消除该提示而把 Visitor URL 改为 loopback 地址。

## 5. 证书续期

Explorer 不签发证书，也不会自动续期。由现有证书工具续期，在成功后的 deploy hook 中将新
`fullchain.pem` 和 `privkey.pem` 更新到**同一个证书目录**，保持文件名、所有者和权限，
然后以原安装账号执行：

```bash
usdb-explorer reload-proxy
usdb-explorer check
```

可以原子替换目录内的两个文件，但不要把整个已挂载目录替换为另一个目录。
两份文件全部更新后再 reload；`reload-proxy` 先执行 `nginx -t`，通过后才重载。
仅更新同路径下的证书内容不需要 down 或 prepare；变更域名、端口或证书目录则需要重新应用部署。
重载后从外部重新连接，核对服务端证书有效期。Nginx 的重载机制见
[官方说明](https://nginx.org/en/docs/control.html)。

生成的内置 Nginx 不提供 ACME HTTP-01 challenge 路由；通配符证书可继续使用已有的 DNS-01
签发/续期设施。不要把证书复制步骤成功当作签发成功，也不要把 reload 成功当作自动续期已建立。

## 常见问题

| 现象 | 检查与处理 |
| --- | --- |
| HTTPS 超时 | 对比本机和公布入口结果；核对外部端口、Docker 发布端口、路由器、防火墙及 A/AAAA |
| `wrong version number` / TLS 协议错误 | HTTPS 请求可能进入 HTTP 28080；核对协议与目标端口 |
| 域名不匹配 | 使用实际证书覆盖的域名访问，检查加载的证书；不能用公网或内网 IP 替代域名 |
| 证书过期或不受信任 | 核对有效期、客户端系统时间、CA 信任和完整中间证书链 |
| `up` 或 `reload-proxy` 的 Nginx 检查失败 | 检查证书文件名、PEM 格式、权限和证书/私钥是否匹配；用 `usdb-explorer logs` 查看已有容器日志 |
| 仍请求旧 HTTP API，或钱包显示旧 RPC | 核对 Visitor URL，执行完整的 prepare 替换流程并强制刷新；更新钱包已有网络设置 |

若要回退到原 HTTP 入口，使用 setup 将 Visitor URL 改回原 HTTP URL，保留 HTTP 28080，
再执行同样的 down、prepare --replace、preflight、up、check 流程，并恢复对应端口映射。
无需删除证书、数据库或水龙头数据。
