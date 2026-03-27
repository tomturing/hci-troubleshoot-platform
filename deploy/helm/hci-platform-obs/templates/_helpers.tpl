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
