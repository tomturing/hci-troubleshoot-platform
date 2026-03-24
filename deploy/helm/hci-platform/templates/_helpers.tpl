{{/*
通用模板辅助函数
*/}}

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
