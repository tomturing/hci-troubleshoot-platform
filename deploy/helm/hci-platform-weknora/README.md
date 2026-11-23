# WeKnora Helm Chart

该 Chart 部署 WeKnora v0.9.0 的完整核心链路，并可启用 Neo4j GraphRAG 与 MinIO
对象存储。本项目的 `full` 口径固定为：frontend、app、docreader、ParadeDB、Redis、
Neo4j、MinIO；不隐式拉起上游 Compose 中用于实验的其他向量库或 Langfuse。

生产环境必须固定所有镜像 tag，并通过环境仓库注入数据库、Redis、JWT、AES、Neo4j
与 MinIO 密钥。`SYSTEM_AES_KEY` 必须长期保持不变，否则数据库中已加密的模型/API Key
无法恢复。

## full 部署示例

```yaml
# values-production.yaml
app:
  replicaCount: 1
  strategy:
    type: Recreate
  env:
    STORAGE_TYPE: minio

dataFiles:
  persistence:
    enabled: true
    size: 10Gi
    accessModes: [ReadWriteOnce]

minio:
  enabled: true
  rootPassword: replace-me
  bucketName: weknora
  persistence:
    enabled: true
    size: 20Gi

neo4j:
  enabled: true
  password: replace-me
  persistence:
    enabled: true
    size: 10Gi

secrets:
  dbPassword: replace-me
  redisPassword: replace-me
  jwtSecret: replace-me-with-random-value
  systemAesKey: replace-with-exactly-32-byte-key
```

共享文件系统部署可在上述 values 之后叠加：

```yaml
# values-shared-storage.yaml
app:
  replicaCount: 3
  strategy:
    type: RollingUpdate

dataFiles:
  persistence:
    enabled: true
    existingClaim: weknora-shared-files
    accessModes: [ReadWriteMany]
```

渲染检查：

```bash
helm template weknora deploy/helm/hci-platform-weknora \
  -n hci-weknora -f values-production.yaml
```

使用 `secrets.existingSecret` 时，Secret 必须包含核心键
`DB_USER`、`DB_PASSWORD`、`DB_NAME`、`REDIS_USERNAME`、`REDIS_PASSWORD`、
`JWT_SECRET`、`SYSTEM_AES_KEY`；启用 MinIO 时还必须包含
`MINIO_ACCESS_KEY_ID`、`MINIO_SECRET_ACCESS_KEY`，启用 Neo4j 时还必须包含
`NEO4J_USERNAME`、`NEO4J_PASSWORD`。
