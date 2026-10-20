{{- define "ambientq.name" -}}{{ .Release.Name }}-ambientq{{- end -}}
{{- define "ambientq.labels" -}}
app.kubernetes.io/name: ambientq
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}
{{- define "ambientq.selector" -}}
app.kubernetes.io/name: ambientq
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
{{- define "ambientq.image" -}}
"{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}"
{{- end -}}

{{/* A quoted string, or null when empty (the config's "unset"). */}}
{{- define "ambientq.strOrNull" -}}
{{- if . }}{{ . | quote }}{{ else }}null{{ end -}}
{{- end -}}

{{/* SQLite on a PVC: single writer, the CronJob must share the server's node. */}}
{{- define "ambientq.sqlitePVC" -}}
{{- if and .Values.persistence.enabled (not .Values.persistence.postgresUrlSecret) }}true{{ end -}}
{{- end -}}

{{/* The dashboard's external URL: auth.publicUrl, else the Ingress host. */}}
{{- define "ambientq.publicUrl" -}}
{{- if .Values.auth.publicUrl -}}
{{ .Values.auth.publicUrl }}
{{- else if .Values.ingress.enabled -}}
{{ if .Values.ingress.tlsSecret }}https{{ else }}http{{ end }}://{{ .Values.ingress.host }}{{ trimSuffix "/" .Values.ingress.path }}
{{- end -}}
{{- end -}}

{{/* Fail early on values that would deploy something broken. */}}
{{- define "ambientq.validate" -}}
{{- $a := .Values.auth -}}
{{- if and (ne $a.mode "none") (not $a.sessionSecret.secretName) -}}
{{- fail "auth.sessionSecret.secretName is required when auth.mode is oidc or proxy" -}}
{{- end -}}
{{- if and (eq $a.mode "oidc") (or (not $a.oidc.issuer) (not $a.oidc.clientId)) -}}
{{- fail "auth.oidc.issuer and auth.oidc.clientId are required when auth.mode is oidc" -}}
{{- end -}}
{{- if and (eq $a.mode "oidc") (not (include "ambientq.publicUrl" .)) -}}
{{- fail "auth.publicUrl (or ingress.enabled with a host) is required for the OIDC redirect URL" -}}
{{- end -}}
{{- if and (gt (int .Values.replicaCount) 1) (not .Values.persistence.postgresUrlSecret) -}}
{{- fail "replicaCount > 1 needs persistence.postgresUrlSecret (SQLite is single-writer)" -}}
{{- end -}}
{{- range $i, $w := .Values.alerts.webhooks -}}
{{- if not $w.secretName -}}
{{- fail (printf "alerts.webhooks[%d].secretName is required (webhook URLs are secrets)" $i) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* Every secret reaches the pods only as an env var from a secretKeyRef. */}}
{{- define "ambientq.env" -}}
- name: AMBIENTQ_CONFIG
  value: /etc/ambientq/ambientq.yaml
- name: NAI_API_KEY
  valueFrom: {secretKeyRef: {name: {{ .Values.nai.apiKeySecret }}, key: {{ .Values.nai.apiKeySecretKey }}}}
{{- if .Values.persistence.postgresUrlSecret }}
- name: AMBIENTQ_STORE_URL
  valueFrom: {secretKeyRef: {name: {{ .Values.persistence.postgresUrlSecret }}, key: url}}
{{- end }}
{{- if .Values.ingestTokenSecret }}
- name: AMBIENTQ_INGEST_TOKEN
  valueFrom: {secretKeyRef: {name: {{ .Values.ingestTokenSecret }}, key: token}}
{{- end }}
{{- with .Values.auth.sessionSecret }}{{ if .secretName }}
- name: AMBIENTQ_SESSION_SECRET
  valueFrom: {secretKeyRef: {name: {{ .secretName }}, key: {{ .key }}}}
{{- end }}{{ end }}
{{- with .Values.auth.oidc.clientSecret }}{{ if .secretName }}
- name: AMBIENTQ_OIDC_CLIENT_SECRET
  valueFrom: {secretKeyRef: {name: {{ .secretName }}, key: {{ .key }}}}
{{- end }}{{ end }}
{{- range $i, $w := .Values.alerts.webhooks }}
- name: AMBIENTQ_ALERT_WEBHOOK_{{ $i }}
  valueFrom: {secretKeyRef: {name: {{ $w.secretName }}, key: {{ $w.key | default "url" }}}}
{{- end }}
{{- with .Values.forge.tokenSecret }}{{ if .secretName }}
- name: FORGE_TOKEN
  valueFrom: {secretKeyRef: {name: {{ .secretName }}, key: {{ .key }}}}
{{- end }}{{ end }}
{{- end -}}

{{- define "ambientq.gitInit" -}}
{{- if .Values.harness.repo }}
- name: harness
  image: {{ include "ambientq.image" . }}
  imagePullPolicy: {{ .Values.image.pullPolicy }}
  command: ["sh", "-c"]
  args:
    - |
      set -e
      URL="{{ .Values.harness.repo }}"
      if [ -n "$GIT_TOKEN" ]; then URL=$(echo "$URL" | sed "s#https://#https://x-access-token:${GIT_TOKEN}@#"); fi
      rm -rf /harness/repo && git clone --depth 50 --branch {{ .Values.harness.ref }} "$URL" /harness/repo
  {{- if .Values.harness.gitSecret }}
  env:
    - name: GIT_TOKEN
      valueFrom: {secretKeyRef: {name: {{ .Values.harness.gitSecret }}, key: token}}
  {{- end }}
  securityContext: {{- toYaml .Values.securityContext | nindent 4 }}
  volumeMounts:
    - {name: harness, mountPath: /harness}
    - {name: tmp, mountPath: /tmp}
{{- end }}
{{- end -}}

{{- define "ambientq.volumeMounts" -}}
- {name: config, mountPath: /etc/ambientq/ambientq.yaml, subPath: ambientq.yaml}
- {name: data, mountPath: /data}
- {name: harness, mountPath: /harness}
- {name: tmp, mountPath: /tmp}
{{- if .Values.nai.caConfigMap }}
- {name: ca, mountPath: /etc/ambientq/ca}
{{- end }}
{{- end -}}

{{- define "ambientq.volumes" -}}
- {name: config, configMap: {name: {{ include "ambientq.name" . }}}}
{{- if include "ambientq.sqlitePVC" . }}
- {name: data, persistentVolumeClaim: {claimName: {{ include "ambientq.name" . }}-data}}
{{- else }}
- {name: data, emptyDir: {}}
{{- end }}
- {name: harness, emptyDir: {}}
- {name: tmp, emptyDir: {}}
{{- if .Values.nai.caConfigMap }}
- {name: ca, configMap: {name: {{ .Values.nai.caConfigMap }}}}
{{- end }}
{{- end -}}

{{- define "ambientq.scheduling" -}}
{{- with .Values.nodeSelector }}
nodeSelector: {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.tolerations }}
tolerations: {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}
