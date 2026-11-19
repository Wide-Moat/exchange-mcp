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

{{/* Whether the key Secret is staged: a key file, a Vault token or an AD FS key. */}}
{{- define "exchange-mcp.stagesSecret" -}}
{{- if or (eq .Values.key.provider "file") (eq .Values.key.provider "vault") (eq .Values.auth.mode "adfs") -}}true{{- end -}}
{{- end -}}

{{/* Whether there is an init container at all: the key Secret, the state store's Secret, or both. */}}
{{- define "exchange-mcp.initContainer" -}}
{{- if or (include "exchange-mcp.stagesSecret" .) .Values.state.existingSecret -}}true{{- end -}}
{{- end -}}

{{/* Whether a shared state store is configured. */}}
{{- define "exchange-mcp.sharedState" -}}
{{- if or .Values.state.url .Values.state.sentinel.hosts -}}true{{- end -}}
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
{{- $many := or (gt (int $v.replicaCount) 1) $v.autoscaling.enabled -}}
{{- if and $many (not (include "exchange-mcp.sharedState" .)) -}}
{{- fail "more than one replica (replicaCount > 1 or autoscaling.enabled) needs state.url or state.sentinel.hosts: without the shared store each pod would accept a sign-in code again and count sign-in attempts and sends on its own. For one pod set replicaCount=1" -}}
{{- end -}}
{{- if and $many (eq $v.key.provider "memory") -}}
{{- fail "key.provider memory holds one key per pod, so a second pod cannot open what the first sealed: use file or vault for more than one replica" -}}
{{- end -}}
{{- if and $v.state.url $v.state.sentinel.hosts -}}{{- fail "state.url and state.sentinel.hosts are two different stores: set one" -}}{{- end -}}
{{- if and $v.state.url (not (or (hasPrefix "redis://" $v.state.url) (hasPrefix "rediss://" $v.state.url))) -}}{{- fail "state.url is redis://host:port/db or rediss://host:port/db" -}}{{- end -}}
{{- if and $v.state.url $v.state.tls (hasPrefix "redis://" $v.state.url) -}}{{- fail "state.tls with state.url: use a rediss:// address" -}}{{- end -}}
{{- if and $v.state.existingSecret (not (include "exchange-mcp.sharedState" .)) -}}{{- fail "state.existingSecret is set but no store is: set state.url or state.sentinel.hosts" -}}{{- end -}}
{{- if and $v.autoscaling.enabled (gt (int $v.autoscaling.minReplicas) (int $v.autoscaling.maxReplicas)) -}}{{- fail "autoscaling.minReplicas is above autoscaling.maxReplicas" -}}{{- end -}}
{{- if eq (int $v.metrics.port) 8000 -}}{{- fail "metrics.port must differ from the server's port 8000: metrics are never served on the public port" -}}{{- end -}}
{{- range $k, $_ := $v.podLabels -}}
{{- if has $k (list "app.kubernetes.io/name" "app.kubernetes.io/instance") -}}{{- fail (printf "podLabels.%s is set by the chart: the Deployment selects its pods by it" $k) -}}{{- end -}}
{{- end -}}
{{- if hasKey $v.podAnnotations "exchange-mcp/key-revision" -}}{{- fail "podAnnotations.exchange-mcp/key-revision is set by the chart from key.revision" -}}{{- end -}}
{{- if and (eq $v.auth.mode "password") (not $v.auth.password.loginDomain) -}}
{{- fail "auth.password.loginDomain is required in password mode" -}}
{{- end -}}
{{- end -}}
