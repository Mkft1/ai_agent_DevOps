{{/* Fully qualified app name (ограничение 63 символа). */}}
{{- define "ai-agent.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/* Имя чарта для label'ов. */}}
{{- define "ai-agent.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* Common labels. */}}
{{- define "ai-agent.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "ai-agent.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/* Selector labels. */}}
{{- define "ai-agent.selectorLabels" -}}
app.kubernetes.io/name: {{ include "ai-agent.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/* Имя ServiceAccount. */}}
{{- define "ai-agent.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "ai-agent.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/* Имя Secret'а с API-ключами (existing или создаваемый чартом). */}}
{{- define "ai-agent.secretName" -}}
{{- if .Values.secrets.existingSecret }}
{{- .Values.secrets.existingSecret }}
{{- else }}
{{- printf "%s-secrets" (include "ai-agent.fullname" .) }}
{{- end }}
{{- end }}

{{/* Имя ConfigMap с не-секретными настройками. */}}
{{- define "ai-agent.configMapName" -}}
{{- printf "%s-config" (include "ai-agent.fullname" .) }}
{{- end }}
