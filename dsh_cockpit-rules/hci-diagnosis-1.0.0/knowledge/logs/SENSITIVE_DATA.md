# 敏感资料说明

以下路径为常见模式，实际位置可能随版本变化。只可在授权范围内用于排障；展示、上传或引用前应脱敏。

| 敏感类别 | 路径模式 | 处理要求 |
|---|---|---|
| 密钥、证书、认证材料 | `cfs/priv/`、`cfs/certs/`、`cfs/*.key`、`sf/cfg/certs/`、`sf/cfg/root-ca/`、`sf/cfg/ssh.d/` | 不输出正文；只说明存在性、权限或验证结果 |
| 用户、权限和登录策略 | `cfs/user*.cfg`、`cfs/role.cfg`、`cfs/acl.cfg`、`cfs/auth/` | 用户名和标识按需掩码 |
| 数据库和导出 | `**/log_new.db`、`cmd-output/*.sql`、其他数据库文件 | 只读限定查询；隐藏账号、令牌和业务名称 |
| 网络和基础设施标识 | `_members`、`cfs/vs/hosts_network_info.json`、网络配置、`etc/hosts` | 对外隐藏内部 IP、MAC、节点名和拓扑 |
| VM 与业务标识 | `cfs/.vmlist`、`cfs/nodes/*/qemu-server/`、任务日志 | 隐藏 VM/业务名称；诊断内部可保留必要 ID |
| Web/API 访问记录 | `sf/log/apache2/`、`sf/log/sf-openapi/`、`sf/log/sangfor_waf/` | 隐藏 Cookie、Authorization、token、客户端地址和请求体 |

禁止复制密钥、口令、完整令牌、证书私钥或大段用户数据到回答。日志知识只保存路径模式、字段和用途，不复制客户现场内容。
