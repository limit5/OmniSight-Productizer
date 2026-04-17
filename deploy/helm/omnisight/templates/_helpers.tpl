{{/*
G5 #5 (TODO row 1373) — Standard Helm helpers.

Aligned with Helm's recommended labels
(https://helm.sh/docs/chart_best_practices/labels/) so the chart-rendered
manifests are byte-faithful to deploy/k8s/*.yaml on the labels operators
read in `kubectl get` / `kubectl describe`.
*/}}

{{/* Chart name (truncated to 63 chars per K8s name limit). */}}
{{- define "omnisight.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Fully-qualified app name. Mirrors deploy/k8s/* `omnisight-backend`
when nameOverride and fullnameOverride are unset (default), so chart
output matches plain manifests for diff parity.
*/}}
{{- define "omnisight.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- printf "%s-backend" $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/* Chart label `<name>-<version>` (Helm recommended). */}}
{{- define "omnisight.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Standard labels set on every chart-rendered object. Matches the
recommended labels on deploy/k8s/* (managed-by flips from `kubectl` to
`Helm` when this chart owns the lifecycle).
*/}}
{{- define "omnisight.labels" -}}
helm.sh/chart: {{ include "omnisight.chart" . }}
{{ include "omnisight.selectorLabels" . }}
app.kubernetes.io/part-of: omnisight
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
{{- end -}}

{{/*
Selector labels — the SUBSET of standard labels that go in
spec.selector.matchLabels on Deployment / Service / PDB. Must be stable
across upgrades (changing a selector breaks the Deployment), so we
deliberately omit `helm.sh/chart` and `app.kubernetes.io/version` here.
*/}}
{{- define "omnisight.selectorLabels" -}}
app.kubernetes.io/name: {{ include "omnisight.fullname" . }}
app.kubernetes.io/component: backend
{{- end -}}

{{/*
ServiceAccount name — uses .Values.serviceAccount.name when set, else
the fullname when serviceAccount.create=true, else "default".
*/}}
{{- define "omnisight.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "omnisight.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}
