{{- define "ambientq.name" -}}{{ .Release.Name }}-ambientq{{- end -}}
{{- define "ambientq.labels" -}}
app.kubernetes.io/name: ambientq
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end -}}
{{- define "ambientq.selector" -}}
app.kubernetes.io/name: ambientq
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
{{- define "ambientq.env" -}}
- name: AMBIENTQ_CONFIG
  value: /etc/ambientq/ambientq.yaml
- name: NAI_API_KEY
  valueFrom: {secretKeyRef: {name: {{ .Values.nai.apiKeySecret }}, key: apiKey}}
{{- if .Values.persistence.postgresUrlSecret }}
- name: AMBIENTQ_STORE_URL
  valueFrom: {secretKeyRef: {name: {{ .Values.persistence.postgresUrlSecret }}, key: url}}
{{- end }}
{{- if .Values.ingestTokenSecret }}
- name: AMBIENTQ_INGEST_TOKEN
  valueFrom: {secretKeyRef: {name: {{ .Values.ingestTokenSecret }}, key: token}}
{{- end }}
{{- end -}}
{{- define "ambientq.securityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
runAsNonRoot: true
runAsUser: 10001
capabilities: {drop: ["ALL"]}
seccompProfile: {type: RuntimeDefault}
{{- end -}}
{{- define "ambientq.gitInit" -}}
{{- if .Values.harness.repo }}
- name: harness
  image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
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
  securityContext: {{- include "ambientq.securityContext" . | nindent 4 }}
  volumeMounts:
    - {name: harness, mountPath: /harness}
    - {name: tmp, mountPath: /tmp}
{{- end }}
{{- end -}}
