# DSH 插件发布制品

本目录只保存由 `dsh_plugins` 源码子项目构建出来的版本化 `.tgz` 发布制品，
不是插件源码的事实来源。插件源码、构建脚本和版本变更在 `dsh_plugins` 项目维护；
构建完成后将对应 `.tgz` 制品复制到本目录，随 DSH 镜像一起发布。

## 插件制品更新流程

1. 在 `dsh_plugins` 对应源码子项目中修改代码或版本，并按该项目的构建流程生成 `.tgz`。
2. 将构建产物复制到本目录，文件名必须包含精确版本号；不要在本目录直接修改解包后的源码。
3. 在 `hci-platform-env/environments/<env>/values.yaml` 或
   `dsh-offline-values.yaml` 更新 `dsh.plugins.packages` 的精确文件名。
4. 构建新的 DSH 镜像；Dockerfile 会把本目录原样复制到镜像内 `/opt/dsh-plugins`。
5. 通过 ArgoCD 滚动更新对应旧 DSH 或离线 DSH 实例。

旧实例与离线实例可以共用镜像制品，但实际安装哪些 bundle 由各自环境 values 的
`dsh.plugins.packages` 独立决定。

## 运营 BI 规则包的原始内容

新离线 DSH 的 Session BI 规则包原始内容不属于 `dsh/plugins/*.tgz` 制品，持久化在仓库根的
`dsh_cockpit-rules/` 下，按规则包 ID 和版本分目录，例如：

```text
dsh_cockpit-rules/
└── hci-diagnosis-1.1.0/
    ├── manifest.json
    ├── workflows/
    └── knowledge/
```

`dsh_cockpit-rules/` 是规则原始内容的事实源；`dsh/plugins/*.tgz` 是插件运行时代码制品，
两者不能互相替代。Session BI 默认使用以下路径，不需要额外声明环境变量：

- 数据文件：`$DSH_HOME/session-bi/records.json`
- 规则包目录：`$DSH_HOME/session-bi/rule-packages`
- 规则源目录：优先 `WECOM_WORKSPACE_ROOT`，否则回退到当前工作目录
- 会话分析目录：优先 `WECOM_RULE_WORKSPACE_ROOT`，否则使用
  `$DSH_HOME/session-bi/work-ticket-rules`

只有需要改变默认布局时，才使用 `DSH_SESSION_BI_DATA_FILE`、
`DSH_SESSION_BI_RULE_PACKAGE_ROOT`、`DSH_SESSION_BI_RULE_SOURCE_ROOT` 或
`DSH_SESSION_BI_ANALYSIS_ROOT` 覆盖。规则内容更新时应保留版本目录，不要覆盖已有版本。

## 本地 bundle 开发

当前两个实例使用的插件组合分别来自 `dsh_v1_plugins` 与 `dsh_v2_plugins`：

| 实例 | 制品 | 用途 |
|---|---:|---|
| v1 / `DSH_PLUGIN_PACKAGES` | `deepseek-harness-wecom-0.1.37.tgz` | 普通 DSH 的企业微信通道（完整 userid）及 Web 工单模板识别 |
| v1 / `DSH_PLUGIN_PACKAGES` | `ip-redactor-0.1.4.tgz` | 旧 DSH 的诊断输出 IP 脱敏插件 |
| v2 / `DSH_OFFLINE_PLUGIN_PACKAGES` | `wecom-0.1.36.tgz` | 新 DSH 的企业微信通道（完整 userid） |
| v2 / `DSH_OFFLINE_PLUGIN_PACKAGES` | `session-bi-0.1.27.tgz` | 会话运营台账、规则包与 Web 首任务工单绑定 |
| v2 / `DSH_OFFLINE_PLUGIN_PACKAGES` | `hci-weknora-diagnosis-bundle-0.2.10.tgz` | WeKnora HCI 诊断 Preset（根 Agent + 并行子 Agent） |
| v2 / `DSH_OFFLINE_PLUGIN_PACKAGES` | `ip-redactor-0.1.4.tgz` | 诊断输出 IP 脱敏插件 |

这些 tgz 是构建上下文中的不可变本地制品；部署 values 必须使用精确文件名，禁止在
容器启动时从 npm registry 解析浮动版本。v2 的 WeKnora Key、知识库 ID 和智能体 ID
通过环境仓 `dsh-offline-plugin-credentials` 注入，不写入公开 values。

> 旧 DSH 实例（v1）的 IP 脱敏自 0.1.4 起与离线实例共用 `ip-redactor-0.1.4.tgz`，
> 不再使用早期的 `dsh-ip-redactor-0.1.3.tgz`。

> `hci-weknora-diagnosis-bundle-0.2.10.tgz` 是标准 npm pack 制品（`package/` 结构），
> 可直接由 `DSH_PLUGIN_PACKAGES` 安装；其依赖 `@wxg-prc-cpg/dsh-weknora@0.1.0`
> 已随 `bundleDependencies` 打进 tgz，安装时不再从 registry 解析。不要把上游发布的「离线整包」
> （`hci-weknora-diagnosis-offline-*`，解包后是目录里再嵌 tgz）放进本目录：它没有
> 包根 `package.json`，`dsh plugin add` 会装成无 manifest 的 `0.0.0` 合成包后失败。
>
> 0.2.10 在 Preset 内启用 `workflow-ptc`（`provider: fork`、3 并发 / 20 总数）与
> `tool-workflow`（`enableRunInBackground: false`）。自 0.2.6 起 Bundle 不再使用 Harness 的
> `taskDrivenUse` 配置字段，而是在 `agent/created` 时用同名 `tool:workflow` 系统提示段覆盖
> 通用用法，因此镜像固定的 `@deepseek-ai/dsh@0.2.1-rc.3` 即可满足（`agent/created` 与
> `getSectionOrder('TOOL_WORKFLOW')` 均已存在）。0.2.10 兼容知识库正文中一层或多层 Markdown
> 下划线转义，并要求报告中任何独立出现的原始案例 ID 都渲染为原文链接。
> Web 新会话的工单绑定依赖 `session-bi-0.1.27`，升级 Bundle 时必须同步升级 Session BI。

> `wecom-0.1.36.tgz` 与 `deepseek-harness-wecom-0.1.37.tgz` 只改了企微入站消息头：
> `userid` 不再截断为前 8 位（原先超过 8 位只显示前 8 位，前缀相同的不同用户会被混淆），
> 其余 `dist` 与上一版一致。两者是**不同的包**（v2 名 `wecom`、v1 名 `deepseek-harness-wecom`），
> 不能装进同一个 profile。

只有尚未进入 `dsh_plugins` 正式构建流程的临时本地 bundle，才放在本目录的子目录，例如：

```text
dsh/plugins/wecom/
├── package.json
├── cordis.patch.yml
└── ...
```

`package.json` 必须声明 `dsh.bundle`，例如：

```json
{
  "name": "dsh-wecom",
  "private": true,
  "dsh": {
    "bundle": {
      "patch": "./cordis.patch.yml"
    }
  }
}
```

在环境仓库 values 中使用容器内路径：

```yaml
dsh:
  plugins:
    packages:
      - /opt/dsh-plugins/wecom
```

Helm 镜像构建会把该目录复制到 `/opt/dsh-plugins`；Docker Compose 会把本地目录只读
挂载到同一路径。插件依赖应在 bundle 自己的 `package.json` 中声明，启动时由
`dsh plugin --profile web add` 安装到 profile。
