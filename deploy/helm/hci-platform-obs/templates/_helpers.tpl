{{/*
通用模板辅助函数（hci-platform-obs 可观测性 Chart 专用）
*/}}

{{/*
Chart 标签
*/}}
{{- define "hci.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version | replace "+" "_" }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: hci-platform
{{- end }}

{{/*
Selector 标签生成器（需传入组件名）
用法: {{ include "hci.selectorLabels" (dict "name" "prometheus" "Release" .Release) }}
*/}}
{{- define "hci.sectorLabels" -}}
app.kubernetes.io/name: {{ .name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "hci.selectorLabels" -}}
app.kubernetes.io/name: {{ .name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
公共 Pod 标签（需传入组件名）
用法: {{ include "hci.podLabels" (dict "name" "loki" "Release" .Release "Chart" .Chart) }}
*/}}
{{- define "hci.podLabels" -}}
{{ include "hci.selectorLabels" . }}
{{ include "hci.labels" . }}
{{- end }}

{{/*
Loki 的配置文件内容。

为什么要把它搬进 Git（2026-10-31）：
在此之前 loki.yaml 只挂载了 `/loki` 数据盘，而 `-config.file=/etc/loki/local-config.yaml`
指向的文件**来自镜像自带**（grafana/loki:3.4.2）。也就是说 retention / limits_config /
compactor 全部不归本仓管理——既改不了、也无法审计，甚至连「线上配置是什么」都要进
容器才知道。更隐蔽的是：因为没有这份文件，「配置有没有变」根本无从比对。

下面这段是**逐字节复制**自现网镜像里的 /etc/loki/local-config.yaml（2026-10-31 抓取，
42 行 / 1138 字节）。刻意不做任何“顺手优化”：先让它与线上完全一致，改动才能被 review
看见；否则「补配置」这一步本身就会变成一次不可见的变更。

retention 现状：**没有** limits_config.retention_period，也没有 compactor.retention_enabled，
即永远不删数据。这是刻意的现状快照而非遗漏——开启删除是不可逆操作，必须单独决策并
同步改文档与 CI 门禁（门禁见 .github/workflows/ci.yml「观测栈渲染不变量」第 4 组断言）。

用法：{{ include "hci.lokiConfig" . }}（configs.yaml 用它生成 ConfigMap，
loki.yaml 用它算 checksum/config —— 单一来源，避免两处漂移）
*/}}
{{- define "hci.lokiConfig" -}}
auth_enabled: false

server:
  http_listen_port: 3100

common:
  instance_addr: 127.0.0.1
  path_prefix: /loki
  storage:
    filesystem:
      chunks_directory: /loki/chunks
      rules_directory: /loki/rules
  replication_factor: 1
  ring:
    kvstore:
      store: inmemory

schema_config:
  configs:
    - from: 2020-10-24
      store: tsdb
      object_store: filesystem
      schema: v13
      index:
        prefix: index_
        period: 24h

ruler:
  alertmanager_url: http://localhost:9093

# By default, Loki will send anonymous, but uniquely-identifiable usage and configuration
# analytics to Grafana Labs. These statistics are sent to https://stats.grafana.org/
#
# Statistics help us better understand how Loki is used, and they show us performance
# levels for most users. This helps us prioritize features and documentation.
# For more information on what's sent, look at
# https://github.com/grafana/loki/blob/main/pkg/usagestats/stats.go
# Refer to the buildReport method to see what goes into a report.
#
# If you would like to disable reporting, uncomment the following lines:
#analytics:
#  reporting_enabled: false
{{- end }}
