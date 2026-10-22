{{/*
通用模板辅助函数（hci-platform-dsh Chart 专用）
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
用法: {{ include "hci.selectorLabels" (dict "name" "dsh" "Release" .Release) }}
*/}}
{{- define "hci.selectorLabels" -}}
app.kubernetes.io/name: {{ .name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
公共 Pod 标签（需传入组件名）
用法: {{ include "hci.podLabels" (dict "name" "dsh" "Release" .Release "Chart" .Chart) }}
*/}}
{{- define "hci.podLabels" -}}
{{ include "hci.selectorLabels" . }}
{{ include "hci.labels" . }}
{{- end }}

{{/*
对外主机名：从 global.publicUrl 中解析裸主机名（去掉协议、端口与路径）
用法: {{ include "dsh.publicHost" . }}
*/}}
{{- define "dsh.publicHost" -}}
{{- $url := .Values.global.publicUrl | default "" -}}
{{- if $url -}}
{{- $url | trimPrefix "https://" | trimPrefix "http://" | regexFind "^[^/:]+" -}}
{{- end -}}
{{- end -}}

{{/*
DSH 对外根地址：publicUrl + ingress.path + "/"
上游要求该地址必须以 / 结尾，且浏览器实际访问的就是它。
用法: {{ include "dsh.publicUrl" . }}
*/}}
{{- define "dsh.publicUrl" -}}
{{ printf "%s%s/" (.Values.global.publicUrl | default "") .Values.ingress.path }}
{{- end -}}
