{{- define "hci-sim.name" -}}
hci-sim
{{- end }}

{{- define "hci-sim.labels" -}}
app.kubernetes.io/name: {{ include "hci-sim.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: hci-platform-test
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version | replace "+" "_" }}
{{- end }}

{{- define "hci-sim.selectorLabels" -}}
app.kubernetes.io/name: {{ include "hci-sim.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
