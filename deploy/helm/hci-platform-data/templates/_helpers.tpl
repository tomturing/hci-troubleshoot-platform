{{/*
hci-platform-data 通用模板辅助函数
命名空间统一使用 .Release.Namespace（由 ArgoCD Application destination.namespace 决定）
*/}}

{{/*
Chart 标签
*/}}
{{- define "hci-data.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version | replace "+" "_" }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: hci-platform
{{- end }}

{{/*
Selector 标签（需传入组件名）
用法: {{ include "hci-data.selectorLabels" (dict "name" "postgres" "Release" .Release) }}
*/}}
{{- define "hci-data.selectorLabels" -}}
app.kubernetes.io/name: {{ .name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Pod 全量标签
用法: {{ include "hci-data.podLabels" (dict "name" "postgres" "Release" .Release "Chart" .Chart) }}
*/}}
{{- define "hci-data.podLabels" -}}
{{ include "hci-data.selectorLabels" . }}
{{ include "hci-data.labels" . }}
{{- end }}
