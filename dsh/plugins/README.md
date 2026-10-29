# 本地 DSH bundle

把本地开发的 DSH bundle 放在本目录的子目录，例如：

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
