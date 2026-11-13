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
实例资源名。旧 release 保持历史名称；第二个 release 通过 namePrefix 隔离资源，
但容器内部名称和 selector 语义仍保持稳定。
*/}}
{{- define "dsh.name" -}}
{{- if .Values.namePrefix }}{{ .Values.namePrefix }}{{ else }}dsh{{ end }}
{{- end }}

{{- define "dsh.proxyName" -}}
{{ include "dsh.name" . }}-proxy
{{- end }}

{{- define "dsh.llmConfigName" -}}
{{ include "dsh.name" . }}-llm-config
{{- end }}

{{- define "dsh.ingressName" -}}
{{ include "dsh.name" . }}-ingress
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
