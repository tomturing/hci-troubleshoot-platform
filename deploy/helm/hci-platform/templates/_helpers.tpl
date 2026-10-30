{{/*
通用模板辅助函数
*/}}

{{/*
C-1 StatefulSet 双重防护 Guard
在 postgres/redis StatefulSet 模板顶部调用：{{ include "hci.dataLayerGuard" . }}
规则：
  - dataLayer.manage=true  → 正常渲染，无限制
  - dataLayer.manage=false → 检查集群中 postgres StatefulSet 是否存在；
      存在且 forceDelete!=true → fail，阻止误删数据层
      存在且 forceDelete=true  → 允许（运维人员明确确认删除数据）
*/}}
{{- define "hci.dataLayerGuard" -}}
{{- if not .Values.dataLayer.manage -}}
  {{- $existing := lookup "apps/v1" "StatefulSet" .Release.Namespace "postgres" -}}
  {{- if $existing -}}
    {{- if not .Values.dataLayer.forceDelete -}}
      {{- fail "危险操作：dataLayer.manage=false 但 postgres StatefulSet 已存在。若要卸载数据层，请先备份数据并设置 dataLayer.forceDelete=true（C-1 双重防护）" -}}
    {{- end -}}
  {{- end -}}
{{- end -}}
{{- end -}}

{{/*
完整名称：release-name 前缀
*/}}
{{- define "hci.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

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
用法: {{ include "hci.selectorLabels" (dict "name" "api-gateway" "Release" .Release) }}
*/}}
{{- define "hci.selectorLabels" -}}
app.kubernetes.io/name: {{ .name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
公共 Pod 标签（需传入组件名和版本）
用法: {{ include "hci.podLabels" (dict "name" "api-gateway" "Release" .Release "Chart" .Chart) }}
*/}}
{{- define "hci.podLabels" -}}
{{ include "hci.selectorLabels" . }}
{{ include "hci.labels" . }}
{{- end }}

{{/*
镜像全名（拼接 registry 前缀）
用法: {{ include "hci.image" (dict "global" .Values.global "image" .Values.apiGateway.image) }}
*/}}
{{- define "hci.image" -}}
{{- if .global.imageRegistry -}}
{{ .global.imageRegistry }}{{ .image.repository }}:{{ .image.tag | default "latest" }}
{{- else -}}
{{ .image.repository }}:{{ .image.tag | default "latest" }}
{{- end -}}
{{- end }}

{{/*
Diagnosis Service（诊断服务）凭据 Secret 名称。
默认由 Chart 创建专用 Secret；受管环境可引用预置 Secret，避免私钥进入 values 仓库。
*/}}
{{- define "hci.diagnosisCredentialsSecretName" -}}
{{- default "diagnosis-service-signing" .Values.diagnosisService.existingCredentialsSecret -}}
{{- end }}

{{/*
阶段一 K3s terminal_bridge 防误配门禁。
SSH 会话目前只保存在单 Pod 内存中；cluster 模式也不能以 wildcard Origin
作为内网 SSH 跳板。把这两个不变量放在 Helm 渲染期检查，避免手工 patch
或错误 values 进入集群后才暴露为运行时故障。
*/}}
{{- define "hci.terminalBridgeGuard" -}}
{{- if .Values.terminalBridge.enabled -}}
  {{- if ne (int .Values.terminalBridge.replicaCount) 1 -}}
    {{- fail "terminalBridge.replicaCount 必须为 1：SSH/WebSocket session 当前保存在单进程内存中" -}}
  {{- end -}}
  {{- $origins := trim (default "" .Values.terminalBridge.allowedOrigins) -}}
  {{- if eq $origins "" -}}
    {{- fail "terminalBridge.enabled=true 时必须配置 terminalBridge.allowedOrigins" -}}
  {{- end -}}
  {{- if contains "*" $origins -}}
    {{- fail "cluster terminal_bridge 禁止 wildcard Origin (*)；请使用 same-origin 或明确的 HTTPS Origin 白名单" -}}
  {{- end -}}
{{- end -}}
{{- end -}}

{{/*
命名空间
*/}}
{{- define "hci.namespace" -}}
{{ .Values.global.namespace | default "hci-troubleshoot" }}
{{- end }}

{{/*
工作负载级 PodSpec 增强（安全与调度）
用法:
  {{ include "hci.workloadPodSpecExtras" (dict "root" . "name" "api-gateway") | nindent 6 }}
*/}}
{{- define "hci.workloadPodSpecExtras" -}}
{{- $root := .root -}}
{{- $name := .name -}}
{{- if and $root.Values.workloadDefaults.securityContext.enabled $root.Values.workloadDefaults.securityContext.pod }}
securityContext:
{{ toYaml $root.Values.workloadDefaults.securityContext.pod | nindent 2 }}
{{- end }}
{{- if and $root.Values.workloadDefaults.scheduling.antiAffinity.enabled }}
affinity:
  podAntiAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
      - weight: {{ default 100 $root.Values.workloadDefaults.scheduling.antiAffinity.weight }}
        podAffinityTerm:
          labelSelector:
            matchLabels:
{{ include "hci.selectorLabels" (dict "name" $name "Release" $root.Release) | nindent 14 }}
          topologyKey: {{ default "kubernetes.io/hostname" $root.Values.workloadDefaults.scheduling.antiAffinity.topologyKey | quote }}
{{- end }}
{{- if and $root.Values.workloadDefaults.scheduling.topologySpread.enabled }}
topologySpreadConstraints:
  - maxSkew: {{ default 1 $root.Values.workloadDefaults.scheduling.topologySpread.maxSkew }}
    topologyKey: {{ default "kubernetes.io/hostname" $root.Values.workloadDefaults.scheduling.topologySpread.topologyKey | quote }}
    whenUnsatisfiable: {{ default "ScheduleAnyway" $root.Values.workloadDefaults.scheduling.topologySpread.whenUnsatisfiable | quote }}
    labelSelector:
      matchLabels:
{{ include "hci.selectorLabels" (dict "name" $name "Release" $root.Release) | nindent 8 }}
{{- end }}
{{- if $root.Values.global.imagePullSecretName }}
imagePullSecrets:
  - name: {{ $root.Values.global.imagePullSecretName }}
{{- end }}
{{- end }}

{{/*
工作负载级容器安全上下文
用法:
  {{ include "hci.workloadContainerSecurityContext" . | nindent 10 }}
*/}}
{{- define "hci.workloadContainerSecurityContext" -}}
{{- if and .Values.workloadDefaults.securityContext.enabled .Values.workloadDefaults.securityContext.container }}
securityContext:
{{ toYaml .Values.workloadDefaults.securityContext.container | nindent 2 }}
{{- end }}
{{- end }}

{{/*
claw pod 专用 DNS 配置（绕过宿主机 Clash TUN fake-ip DNS 劫持，参考 PIT-034）
所有需要出网访问 AI provider 的 claw pod（openclaw/learningclaw/productionclaw）
均应在 pod spec 中 include 此 helper。
用法:
  {{ include "hci.clawDnsConfig" . }}
*/}}
{{- define "hci.clawDnsConfig" -}}
dnsPolicy: None
dnsConfig:
  nameservers:
    - 114.114.114.114
    - 1.2.4.8
  options:
    - name: ndots
      value: "1"
{{- end }}

{{/*
J-3 Pod 安全上下文基线（OWASP K8s Top 10 合规快速引用）
此 helper 是 hci.workloadPodSpecExtras 的摘要版，用于无法使用 workloadDefaults 的场景。
用法:
  {{ include "hci.podSecurityContext" . | nindent 6 }}
*/}}
{{- define "hci.podSecurityContext" -}}
securityContext:
  runAsNonRoot: true
  runAsUser: 1000
  runAsGroup: 1000
  fsGroup: 1000
  seccompProfile:
    type: RuntimeDefault
{{- end }}

{{/*
J-3 容器安全上下文基线
用法:
  {{ include "hci.containerSecurityContext" . | nindent 10 }}
*/}}
{{- define "hci.containerSecurityContext" -}}
securityContext:
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: false
  capabilities:
    drop:
      - ALL
{{- end }}

{{/*
抽象助手注册表 JSON（hci.assistantRegistry）
从顶级 llm.* 配置动态生成，确保 htp-agent 的 base_url / model 与 llm 配置一致。
ops-agent 和 pai-agent 为内部服务，base_url 固定不变。
用法: {{ include "hci.assistantRegistry" . | quote }}
*/}}
{{- define "hci.assistantRegistry" -}}
{
  "htp-agent": {
    "base_url": "{{ .Values.llm.baseUrl }}",
    "model": "{{ .Values.llm.model }}",
    "warm_pool_size": 0,
    "max_pool_size": 0,
    "display_name": "HTP Agent",
    "description": "HCI智能排障平台核心助手",
    "is_default": true,
    "enabled": true
  },
  "ops-agent": {
    "warm_pool_size": 0,
    "max_pool_size": 0,
    "base_url": "http://ops-agent-service:8006",
    "display_name": "OPS Agent",
    "description": "基于SOP知识库的智能排障助手",
    "enabled": true
  },
  "pai-agent": {
    "base_url": "http://conversation-service:8002",
    "warm_pool_size": 0,
    "max_pool_size": 0,
    "display_name": "PAI Agent",
    "description": "基于pydantic-ai框架的排障助手",
    "enabled": true,
    "is_default": false,
    "capabilities": ["troubleshooting", "tool-calling"]
  }
}
{{- end -}}

{{/*
停止监听前的强制等待（preStop），供所有 Deployment 的容器共用。

第一性原理：请求会不会打到「正在死的 Pod」，取决于两个互相独立的时钟——
  时钟 A：Pod 拿到 deletionTimestamp → 从 EndpointSlice 消失 → 经 Traefik /
          kube-proxy 各自的速度传播到每一条转发路径（staging 实测约 2s）；
  时钟 B：进程收到 SIGTERM → 停止 accept（uvicorn 是毫秒级）。
只有 A < B 才没有 502。maxUnavailable=0 回答的是「还有没有别的 Pod 在服务」，
回答不了「流量是不是正好打在已经闭眼的那个上」，这两件事经常被混为一谈。

2026-10-30 staging 实测（无 preStop，经 Traefik 施压 2338 次）：旧 Pod 被
Killing 的同一秒起连续 3 秒出现 28 个 502，而 endpoints 全程非空。

取值：默认读 global.preStopSleepSeconds（10s，对实测 2s 留 5 倍余量）；
单个服务可用 <serviceKey>.preStopSleepSeconds 覆盖，置 0 即关闭。
必须小于 terminationGracePeriodSeconds，否则 preStop 先吃掉优雅期、超时后
kubelet 直接 SIGKILL，在途请求照样丢——所以这里直接 fail 掉而不是交给人工检查。

用法：{{- include "hci.preStopLifecycle" (dict "root" . "key" "adminUI") | nindent 10 }}
*/}}
{{- define "hci.preStopLifecycle" -}}
{{- $svc := index .root.Values .key | default dict -}}
{{- $sleep := .root.Values.global.preStopSleepSeconds | default 0 | int -}}
{{- if hasKey $svc "preStopSleepSeconds" -}}
{{- $sleep = get $svc "preStopSleepSeconds" | int -}}
{{- end -}}
{{- $grace := get $svc "terminationGracePeriodSeconds" | default 30 | int -}}
{{- if gt $sleep 0 -}}
{{- if ge $sleep $grace -}}
{{- fail (printf "preStopSleepSeconds=%d 必须小于 terminationGracePeriodSeconds=%d：preStop 会先吃掉优雅期，超时后 kubelet 直接 SIGKILL，在途请求照样丢" $sleep $grace) -}}
{{- end -}}
# 等负载均衡视图更新完再停止监听（详见 _helpers.tpl 的 hci.preStopLifecycle）
lifecycle:
  preStop:
    exec:
      command: ["/bin/sh", "-c", "sleep {{ $sleep }}"]
{{- end -}}
{{- end -}}
