#!/usr/bin/env bash
# Version pipeline of the update policy (design section 3.5):
#   1. read every cluster's current Kubernetes version and node image (Resource Graph);
#   2. update the release catalog with versions first seen in the region (az aks get-versions);
#   3. compute the due steps (aks-agent versions) – deterministic, no LLM;
#   4. start the upgrade agent once per environment that has due steps, with the steps as its trigger.
# Needs: az logged in with a read identity, aks-agent installed, the catalog in blob storage.
#   env: REGION, ENVIRONMENTS ("dev:<sub-id> acc:<sub-id> prd:<sub-id>"), CATALOG_ACCOUNT, CATALOG_CONTAINER,
#        AGENTS_RESOURCE_GROUP, AGENTS_SUBSCRIPTION, DRY_RUN (true = only print the steps)
set -euo pipefail
work=$(mktemp -d)
cd "$work"

az storage blob download --account-name "$CATALOG_ACCOUNT" -c "$CATALOG_CONTAINER" -n aks-releases.yaml \
  -f aks-releases.yaml --auth-mode login --only-show-errors >/dev/null 2>&1 || echo "{}" > aks-releases.yaml

echo "[]" > clusters.json
: > node-images.txt
for pair in $ENVIRONMENTS; do
  env=${pair%%:*} sub=${pair#*:}
  az graph query --subscriptions "$sub" --first 100 -o json -q "
    resources
    | where type =~ 'microsoft.containerservice/managedclusters'
    | project name, resourceGroup,
              kubernetes = tostring(properties.currentKubernetesVersion),
              node_image = tostring(properties.agentPoolProfiles[0].nodeImageVersion),
              pool = tostring(properties.agentPoolProfiles[0].name)" \
  | jq --arg env "$env" '[.data[] | {environment: $env, name, resourceGroup, kubernetes, node_image, pool}]' > "$env.json"
  jq -s 'add' clusters.json "$env.json" > all.json && mv all.json clusters.json
  # The latest node image AKS offers, from one cluster of the environment.
  first=$(jq -r '.[0] | select(.) | "\(.resourceGroup) \(.name) \(.pool)"' "$env.json")
  if [ -n "$first" ]; then
    read -r rg name pool <<< "$first"
    az aks nodepool get-upgrades --subscription "$sub" -g "$rg" --cluster-name "$name" --nodepool-name "$pool" \
      --query latestNodeImageVersion -o tsv >> node-images.txt || true
  fi
done

az aks get-versions -l "$REGION" -o json > get-versions.json
aks-agent catalog --catalog aks-releases.yaml --get-versions get-versions.json --node-images node-images.txt
aks-agent versions --catalog aks-releases.yaml --clusters clusters.json > steps.json
cat steps.json

if [ "${DRY_RUN:-false}" != "true" ]; then
  az storage blob upload --account-name "$CATALOG_ACCOUNT" -c "$CATALOG_CONTAINER" -n aks-releases.yaml \
    -f aks-releases.yaml --overwrite --auth-mode login --only-show-errors >/dev/null
  for env in $(jq -r '[.steps[] | select(.action == "upgrade") | .environment] | unique | .[]' steps.json); do
    trigger=$(jq -c --arg env "$env" '{event: "version-pipeline", today, steps: [.steps[] | select(.environment == $env)]}' steps.json)
    az containerapp job start --subscription "$AGENTS_SUBSCRIPTION" -g "$AGENTS_RESOURCE_GROUP" \
      -n "job-upgrade-agent-$env" --container-name agent \
      --env-vars "AKS_AGENT_TASK=Open the version pull requests for the due steps in $env." \
                 "AKS_AGENT_TRIGGER_JSON=$trigger" --only-show-errors
  done
fi
