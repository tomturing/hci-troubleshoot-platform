# 关键信号 Catalog 管理与在线热加载方案

## 1. 背景与目标

在 HCI 故障排查平台中，Shared Resolution Runtime 依赖两个核心 Catalog 配置文件：
- `acli_command_catalog.json`：定义系统所支持的 336+ 条 aCLI 命令规范与路径前缀；
- `resolution_catalog.json`：包含 Log 文件别名映射、Domain 命令必填参数契约以及 QKV 动作同义词关联。

此前这些 Catalog 存在副本散落（`agent-service` 与 `kb-service` 重复维护）、路径查找错误导致 Pod 中出现 `SYSTEM_COMMAND_UNKNOWN` 阻断告警的问题。本方案将 Catalog 权威存储收聚至单一目录，实现线程安全的免重启热重载，并在 Admin UI 提供可视化的管理与在线编辑功能。

---

## 2. 架构设计与权威存储

### 2.1 唯一权威存储路径
配置文件的唯一源码路径收聚至：
`backend/shared/resolution/catalogs/`
- `acli_command_catalog.json`
- `resolution_catalog.json`

后端所有微服务镜像打包时均通过 `COPY backend/shared /app/shared` 将此目录一同部署，严禁在 `agent-service` 或 `kb-service` 中保留独立的硬编码副本。

### 2.2 线程安全热重载 (`_HotCatalog`)
在 `shared/resolution/catalog.py` 中实现了基于文件修改时间 (`st_mtime`) 的 `_HotCatalog` 缓存：
- **感知增量**：每次获取时自动比对磁盘文件 `mtime`，未变更则直接返回内存 Tuple/Dict；
- **免重启生效**：在线修改并保存 JSON 文件写回磁盘后，系统在下一次审查或调用时**无需重启微服务即可即时感应生效**；
- **容错防坍塌**：文件不存在或 JSON 格式非法时，保留上一次成功加载的有效缓存，避免破坏运行时服务。

---

## 3. Admin UI 管理页面与 REST API

### 3.1 后端 REST API
- `GET /api/kb/resolution-catalogs`：获取可管理的 Catalog 列表与修改时间、记录条数；
- `GET /api/kb/resolution-catalogs/{filename}`：获取指定 Catalog 的 JSON 文本与结构化对象；
- `POST /api/kb/resolution-catalogs/{filename}/validate`：在线校验输入 JSON 的语法与规则格式；
- `PUT /api/kb/resolution-catalogs/{filename}`：更新 Catalog 内容写回磁盘，即刻触发热重载。

### 3.2 前端控制台 (Admin UI `/catalog`)
- **路由位置**：挂载于“分类基线”正下方 (order 6.5，图标 `Collection`)。
- **模式切换**：支持“结构化卡片视图”与“JSON 源码编辑器”双模式切换。
- **即时写回与自动热生效**：结构化视图中的“确定添加”与“删除”操作自动触发后端 REST API，即时保存写回磁盘文件并触发 shared resolution runtime 热加载。
- **容器写权限保障**：Dockerfile 补充 `RUN chmod -R 777 /app/shared`，彻底避免非 root 运行容器的 Errno 13 权限隐患。：
  - **结构化视图**：提供表格与卡片化编辑界面（支持 aCLI 命令列表搜索过滤、新增/修改/删除，Log 别名映射，Domain 约束与 QKV Action 管理）；
  - **源码编辑器**：提供高亮文本区、`Format JSON` 格式化美化按钮与实时语法校验提示。
- **导入/导出**：支持选择本地 `.json` 上传覆盖编辑，支持一键导出下载 JSON 文件。
- **保存生效**：点击【保存并热生效】直接提交写回后端并触发即时加载。
