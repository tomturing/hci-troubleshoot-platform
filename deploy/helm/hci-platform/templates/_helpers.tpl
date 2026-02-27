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
