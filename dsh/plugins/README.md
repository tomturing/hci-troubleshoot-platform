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
└── hci-diagnosis-1.6.1-rc.1/
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

镜像中同时保留旧诊断服务的 `deepseek-harness-wecom-offline-0.1.22.tgz`，以及
新「HCI 单 Agent 离线诊断」实例使用的最新兼容组合：

| 插件 | 版本 | 用途 |
|---|---:|---|
| `deepseek-harness-wecom` | `0.1.34` | 企业微信通道；多于三个确认项时拆分多张卡片并保持同一会话答案状态 |
| `deepseek-harness-session-bi` | `0.1.21` | 会话运营台账与规则包 |
| `hci-offline-diagnosis-bundle` | `0.4.2` | HCI 单 Agent 离线诊断 Preset；移除 DSH 0.2.1 未提供的执行器插件 |

这些 tgz 是构建上下文中的不可变本地制品；部署 values 必须使用精确文件名，禁止在
容器启动时从 npm registry 解析浮动版本。

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
