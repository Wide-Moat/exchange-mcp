{{- define "exchange-mcp.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "exchange-mcp.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "exchange-mcp.selectorLabels" -}}
app.kubernetes.io/name: {{ include "exchange-mcp.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "exchange-mcp.labels" -}}
{{ include "exchange-mcp.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}

{{- define "exchange-mcp.image" -}}
{{- if .Values.image.digest -}}
{{ .Values.image.repository }}@{{ .Values.image.digest }}
{{- else -}}
{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}
{{- end -}}
{{- end -}}

{{/* Whether the init container has a Secret to stage: a key file, a Vault token or an AD FS key. */}}
{{- define "exchange-mcp.stagesSecret" -}}
{{- if or (eq .Values.key.provider "file") (eq .Values.key.provider "vault") (eq .Values.auth.mode "adfs") -}}true{{- end -}}
{{- end -}}

{{- define "exchange-mcp.validate" -}}
{{- $v := .Values -}}
{{- if not (hasPrefix "https://" $v.publicUrl) -}}{{- fail "publicUrl is required and must start with https://" -}}{{- end -}}
{{- if not (hasPrefix "https://" $v.exchange.ewsUrl) -}}{{- fail "exchange.ewsUrl is required and must start with https://" -}}{{- end -}}
{{- if not (has $v.key.provider (list "file" "vault" "memory")) -}}{{- fail "key.provider is file, vault or memory" -}}{{- end -}}
{{- if not (has $v.auth.mode (list "password" "adfs")) -}}{{- fail "auth.mode is password or adfs" -}}{{- end -}}
{{- if and (include "exchange-mcp.stagesSecret" .) (not $v.key.existingSecret) -}}
{{- fail "key.existingSecret is required: the Secret CI writes with the key (and the Vault token or AD FS key)" -}}
{{- end -}}
{{- if gt (int $v.replicaCount) 1 -}}
{{- fail "replicaCount must be 1: used sign-in codes and sign-in attempt counts are kept per process, so a second pod would accept a code twice and double the attempts before a lockout" -}}
{{- end -}}
{{- range $k, $_ := $v.podLabels -}}
{{- if has $k (list "app.kubernetes.io/name" "app.kubernetes.io/instance") -}}{{- fail (printf "podLabels.%s is set by the chart: the Deployment selects its pods by it" $k) -}}{{- end -}}
{{- end -}}
{{- if hasKey $v.podAnnotations "exchange-mcp/key-revision" -}}{{- fail "podAnnotations.exchange-mcp/key-revision is set by the chart from key.revision" -}}{{- end -}}
{{- if and (eq $v.auth.mode "password") (not $v.auth.password.loginDomain) -}}
{{- fail "auth.password.loginDomain is required in password mode" -}}
{{- end -}}
{{- end -}}
