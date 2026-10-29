#!/usr/bin/env bash
# monitoring.yaml 의 PrometheusRule 규칙을 promtool 단위 시험(monitoring_test.yaml)으로 검증한다.
#   bash apps/interview-coach/promtool-test.sh
# monitoring.yaml 은 ServiceMonitor + PrometheusRule 을 한 파일에 담은 쿠버네티스 매니페스트라 promtool 이
# 그대로 못 읽는다 — 여기서 PrometheusRule.spec 만 뽑아 monitoring.rules.generated.yaml 로 만든 뒤 지운다.
# yq · promtool 이 로컬에 있으면 그걸 쓰고, 없으면 docker(mikefarah/yq · prom/prometheus)로 대신한다.
set -euo pipefail
cd "$(dirname "$0")"
trap 'rm -f monitoring.rules.generated.yaml' EXIT

if command -v yq >/dev/null 2>&1; then
  yq eval-all 'select(.kind == "PrometheusRule") | .spec' monitoring.yaml > monitoring.rules.generated.yaml
else
  docker run --rm -v "$PWD":/data mikefarah/yq eval-all \
    'select(.kind == "PrometheusRule") | .spec' /data/monitoring.yaml > monitoring.rules.generated.yaml
fi

if command -v promtool >/dev/null 2>&1; then
  promtool test rules monitoring_test.yaml
else
  docker run --rm --entrypoint promtool -v "$PWD":/data prom/prometheus:v2.55.1 test rules /data/monitoring_test.yaml
fi
