#!/usr/bin/env bash
# deploy-lke.sh
#
# Deploys the Linode Instance Scheduler onto an LKE cluster: the scheduler/API Deployment,
# its PVC, ConfigMap, and Secrets, all from this directory. Run this instead of
# `kubectl apply -f` on individual files unless you specifically know you want to bypass its
# checks -- it refuses to run without real (not `.example`) Secret files present, and it
# handles the one genuine integration point with the sibling
# github.com/sandipgangdhar/Linode-LKE-Private-Network project (see "VLAN gating detection"
# below) that a bare `kubectl apply` would silently skip.
#
# -----------------------------------------------------
# What this does NOT do, on purpose:
#
# This script never reads, imports, or depends on anything from
# Linode-LKE-Private-Network's own code, etcd, or Kubernetes objects beyond the read-only
# `kubectl get`/`kubectl label` calls below. The two projects solve unrelated problems (that
# one gives LKE nodes a private VLAN/VPC network interface; this one deletes/recreates
# standalone Linode instances on a schedule) and stay fully independent as code. The ONLY
# thing this script's detection step changes is whether this deployment's own namespace
# carries the `kyverno-mutation-exempt=true` label -- see "VLAN gating detection" below for
# exactly what that does and why it's the one real touchpoint worth asking about.
#
# -----------------------------------------------------
# Usage:
#
#   ./deploy-lke.sh --image ghcr.io/sandipgangdhar/linode-instance-scheduler:latest [options]
#
# (the official image -- see docs/deployment-guide-lke.md §3 -- or your own
# <registry>/linode-instance-scheduler:... if you built one yourself)
#
# Options:
#   --namespace NAME       Kubernetes namespace to deploy into (default: linode-instance-scheduler)
#   --expose MODE          cluster-ip (default) or loadbalancer -- see service.clusterip.yaml/
#                           service.loadbalancer.yaml's own header comments for the tradeoff
#   --assume-gated         Skip the interactive VLAN-gating question; deploy WITHOUT the
#                           kyverno-mutation-exempt label (this pod will require a
#                           vlan-ready=true node if Linode-LKE-Private-Network's gate is present)
#   --assume-exempt         Skip the interactive VLAN-gating question; deploy WITH the
#                           kyverno-mutation-exempt label (this pod schedules normally
#                           regardless of any VLAN gate present)
#   --yes                  Non-interactive. If neither --assume-gated nor --assume-exempt was
#                           also given, defaults to --assume-exempt (the least-surprising
#                           choice for an unattended/scripted deploy -- see below)
#   --cleanup               Delete the Deployment/Services/ConfigMap/Secrets this script
#                           manages. Does NOT delete the PVC (your data) or the namespace by
#                           default -- add --delete-data / --delete-namespace for that
#   --delete-data           With --cleanup: also delete the PVC (destroys the SQLite
#                           registry/state permanently -- the underlying Linode Block
#                           Storage volume itself survives if pvc.yaml's storageClassName
#                           uses a Retain reclaim policy, per its own comment)
#   --delete-namespace      With --cleanup: also delete the whole namespace
#
# Requires: kubectl (configured against your target cluster), envsubst (part of GNU
# gettext -- same dependency the sibling Linode-LKE-Private-Network project's own
# orchestration script already has for its ${ETCD_ENDPOINTS} placeholder).
#
# -----------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

log() {
  echo -e "[INFO] $(date '+%Y-%m-%d %H:%M:%S') $1"
}

# === Argument parsing ===
NAMESPACE="linode-instance-scheduler"
IMAGE=""
EXPOSE="cluster-ip"
ASSUME=""       # "" | "gated" | "exempt"
NON_INTERACTIVE="false"
DO_CLEANUP="false"
DELETE_DATA="false"
DELETE_NAMESPACE="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace) NAMESPACE="$2"; shift 2 ;;
    --image) IMAGE="$2"; shift 2 ;;
    --expose) EXPOSE="$2"; shift 2 ;;
    --assume-gated) ASSUME="gated"; shift ;;
    --assume-exempt) ASSUME="exempt"; shift ;;
    --yes) NON_INTERACTIVE="true"; shift ;;
    --cleanup) DO_CLEANUP="true"; shift ;;
    --delete-data) DELETE_DATA="true"; shift ;;
    --delete-namespace) DELETE_NAMESPACE="true"; shift ;;
    -h|--help) sed -n '2,55p' "$0"; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [[ "$EXPOSE" != "cluster-ip" && "$EXPOSE" != "loadbalancer" ]]; then
  echo "ERROR: --expose must be 'cluster-ip' or 'loadbalancer', got: $EXPOSE" >&2
  exit 1
fi

for bin in kubectl envsubst; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "ERROR: required command not found: $bin" >&2
    exit 1
  fi
done

# === --cleanup ===
if [[ "$DO_CLEANUP" == "true" ]]; then
  log "Cleaning up namespace $NAMESPACE ..."
  kubectl delete service linode-instance-scheduler-api -n "$NAMESPACE" --ignore-not-found
  kubectl delete deployment linode-instance-scheduler -n "$NAMESPACE" --ignore-not-found
  kubectl delete configmap linode-instance-scheduler-config -n "$NAMESPACE" --ignore-not-found
  kubectl delete secret linode-instance-scheduler-secrets -n "$NAMESPACE" --ignore-not-found
  kubectl delete secret linode-instance-scheduler-ssh-key -n "$NAMESPACE" --ignore-not-found
  if [[ "$DELETE_DATA" == "true" ]]; then
    log "Deleting PVC linode-instance-scheduler-state (--delete-data was given) ..."
    kubectl delete pvc linode-instance-scheduler-state -n "$NAMESPACE" --ignore-not-found
  else
    log "Leaving PVC linode-instance-scheduler-state in place (pass --delete-data to remove it too)."
  fi
  if [[ "$DELETE_NAMESPACE" == "true" ]]; then
    log "Deleting namespace $NAMESPACE (--delete-namespace was given) ..."
    kubectl delete namespace "$NAMESPACE" --ignore-not-found
  fi
  log "Cleanup complete."
  exit 0
fi

if [[ -z "$IMAGE" ]]; then
  echo "ERROR: --image is required, e.g. --image ghcr.io/sandipgangdhar/linode-instance-scheduler:latest (the official image), or your own <registry>/linode-instance-scheduler:... -- see the repo root Dockerfile." >&2
  exit 1
fi

# === Confirm cluster access before touching anything ===
log "Checking kubectl access to the current context ($(kubectl config current-context 2>/dev/null || echo '<none>')) ..."
if ! kubectl get nodes >/dev/null; then
  echo "ERROR: kubectl cannot reach a cluster in the current context. Run 'kubectl config current-context' / 'kubectl config use-context <name>' first." >&2
  exit 1
fi

# === Refuse to run against uncustomized/missing secret templates ===
# Same convention Linode-LKE-Private-Network's own 00-Orchestration-script.sh uses for its
# ConfigMap/Secret: an uncustomized copy of a *.example.yaml template would either fail
# outright (an empty LINODE_API_TOKEN) or, worse, silently misconfigure the deployment --
# better to refuse clearly, up front, than partially apply something broken.
for real in secret.yaml ssh-key-secret.yaml; do
  if [[ ! -f "$real" ]]; then
    echo "ERROR: $SCRIPT_DIR/$real not found. Copy ${real%.yaml}.example.yaml to $real and fill in real values first (see k8s/README.md)." >&2
    exit 1
  fi
done
# configmap.yaml has no secrets in it -- auto-provision it from the example on first run
# instead of forcing an extra manual copy step for a file that's safe to apply as-is.
if [[ ! -f configmap.yaml ]]; then
  log "configmap.yaml not found -- using configmap.example.yaml's defaults as-is (copy it to configmap.yaml first if you want to customize POLL_INTERVAL_SECONDS/POLL_WINDOW_SECONDS/API_ALLOWED_ORIGINS)."
  cp configmap.example.yaml configmap.yaml
fi

# === VLAN gating detection ===
#
# Checks, read-only, whether this cluster is already running
# github.com/sandipgangdhar/Linode-LKE-Private-Network -- the same core-resource-existence
# signature that project's own 00-Orchestration-script.sh (is_fresh_deploy()) uses to tell a
# fresh deploy from a re-run against its own components, applied here from the outside
# instead. None of these calls mutate anything.
LKE_PN_COMPONENTS_FOUND="false"
for check in \
  "statefulset/etcd" \
  "daemonset/vlan-manager" \
  "deployment/vlan-config-controller" \
  "deployment/vlan-ip-controller"
do
  if kubectl get "$check" -n kube-system >/dev/null 2>&1; then
    LKE_PN_COMPONENTS_FOUND="true"
    log "Detected: $check (kube-system) -- this cluster appears to be running Linode-LKE-Private-Network."
  fi
done
if [[ "$LKE_PN_COMPONENTS_FOUND" == "false" ]]; then
  log "No Linode-LKE-Private-Network components detected on this cluster."
fi

# The gate itself is the one thing that actually affects THIS deployment -- checked
# separately from the components above (a cluster could theoretically have one without the
# other, e.g. mid-migration). Kyverno ships the gate as one of two mutually-exclusive policy
# types depending on Kyverno's own version -- see that project's
# 09-kyverno-vlan-ready-mutatingpolicy.yaml header for the full CEL-vs-legacy-ClusterPolicy
# history. Check both names; at most one will ever actually exist on a given cluster.
GATE_PRESENT="false"
GATE_KIND=""
if kubectl get mutatingpolicy linode-lke-vlan-gating >/dev/null 2>&1; then
  GATE_PRESENT="true"
  GATE_KIND="MutatingPolicy (policies.kyverno.io/v1)"
elif kubectl get clusterpolicy linode-lke-vlan-gating >/dev/null 2>&1; then
  GATE_PRESENT="true"
  GATE_KIND="ClusterPolicy (kyverno.io/v1, legacy)"
fi

if [[ "$GATE_PRESENT" == "true" ]]; then
  log "Detected: Kyverno $GATE_KIND 'linode-lke-vlan-gating' -- every pod created in a non-excluded namespace on this cluster is automatically mutated to require a vlan-ready=true node, unless that namespace is labeled kyverno-mutation-exempt=true."
fi

EXEMPT="true"  # irrelevant unless GATE_PRESENT=true, but keep a defined value either way
if [[ "$GATE_PRESENT" == "true" ]]; then
  if [[ -n "$ASSUME" ]]; then
    EXEMPT="$([[ "$ASSUME" == "exempt" ]] && echo true || echo false)"
    log "Using --assume-$ASSUME (skipping the interactive question)."
  elif [[ "$NON_INTERACTIVE" == "true" ]]; then
    log "Non-interactive (--yes) with no --assume-gated/--assume-exempt given -- defaulting to exempt (the pod will schedule normally, ignoring the VLAN gate). Pass --assume-gated explicitly if you want the opposite for a scripted deploy."
    EXEMPT="true"
  else
    echo ""
    echo "This cluster is running Linode-LKE-Private-Network's VLAN gating ($GATE_KIND)."
    echo "It automatically mutates every new pod (outside a short exclude-list of namespaces)"
    echo "to require scheduling onto a node labeled vlan-ready=true, and to tolerate the"
    echo "permanent vlan-not-ready taint -- with no action needed from this deployment's own"
    echo "manifests either way."
    echo ""
    echo "Do you want the Linode Instance Scheduler's pod to run UNDER that gate?"
    echo "  1) No  (recommended unless you have a specific reason) -- exempt this namespace"
    echo "     (kyverno-mutation-exempt=true). The pod schedules normally on any node; nothing"
    echo "     about this tool's own job (talking to the public Linode API to manage other"
    echo "     instances) needs a VLAN/VPC interface for itself."
    echo "  2) Yes -- run under the gate like any other application pod on this cluster. Use"
    echo "     this only if you specifically want the scheduler's own pod reachable over your"
    echo "     existing private VLAN/NAT path, or your cluster convention requires every"
    echo "     workload to be VLAN-gated."
    echo ""
    read -r -p "Choice [1/2, default 1]: " choice
    case "${choice:-1}" in
      2) EXEMPT="false" ;;
      *) EXEMPT="true" ;;
    esac
  fi
fi

# === Apply namespace, with or without the exempt label ===
log "Applying namespace $NAMESPACE ..."
NAMESPACE_YAML="$(sed "s/name: linode-instance-scheduler$/name: $NAMESPACE/" namespace.yaml)"
echo "$NAMESPACE_YAML" | kubectl apply -f -

if [[ "$GATE_PRESENT" == "true" && "$EXEMPT" == "true" ]]; then
  log "Labeling namespace $NAMESPACE kyverno-mutation-exempt=true ..."
  kubectl label namespace "$NAMESPACE" kyverno-mutation-exempt=true --overwrite
elif [[ "$GATE_PRESENT" == "true" && "$EXEMPT" == "false" ]]; then
  log "Removing any stale kyverno-mutation-exempt label from namespace $NAMESPACE (running under the gate) ..."
  kubectl label namespace "$NAMESPACE" kyverno-mutation-exempt- --ignore-not-found 2>/dev/null || true
fi

# === Apply ConfigMap/Secrets/PVC/Deployment/Service ===
# --namespace override: every manifest in this directory hardcodes
# `namespace: linode-instance-scheduler` for readability -- rewrite it on the fly here if a
# different --namespace was requested, rather than requiring every file to be hand-edited.
apply_in_namespace() {
  sed "s/namespace: linode-instance-scheduler$/namespace: $NAMESPACE/" "$1" | kubectl apply -f -
}

log "Applying ConfigMap ..."
apply_in_namespace configmap.yaml

log "Applying Secrets ..."
apply_in_namespace secret.yaml
apply_in_namespace ssh-key-secret.yaml

log "Applying PersistentVolumeClaim ..."
apply_in_namespace pvc.yaml

log "Applying Deployment (image: $IMAGE) ..."
# The single-quoted '${IMAGE}' below is envsubst's own variable-name filter argument
# (limiting substitution to just this one var, so nothing else that happens to look like
# ${...} in the manifest gets touched), not a shell expansion -- it must stay single-quoted
# for envsubst to receive it literally.
# shellcheck disable=SC2016
IMAGE="$IMAGE" envsubst '${IMAGE}' < deployment.yaml | sed "s/namespace: linode-instance-scheduler$/namespace: $NAMESPACE/" | kubectl apply -f -

SERVICE_FILE="service.clusterip.yaml"
if [[ "$EXPOSE" == "loadbalancer" ]]; then
  SERVICE_FILE="service.loadbalancer.yaml"
fi
log "Applying Service ($SERVICE_FILE) ..."
apply_in_namespace "$SERVICE_FILE"

log "Waiting for the Deployment to become ready ..."
kubectl rollout status deployment/linode-instance-scheduler -n "$NAMESPACE" --timeout=180s

log "Deployed. Verify with:"
echo "  kubectl get pods -n $NAMESPACE"
echo "  kubectl logs -n $NAMESPACE deploy/linode-instance-scheduler -c scheduler -f"
echo "  kubectl exec -n $NAMESPACE deploy/linode-instance-scheduler -c api -- curl -s http://localhost:8000/health"
if [[ "$EXPOSE" == "loadbalancer" ]]; then
  echo "  kubectl get service linode-instance-scheduler-api -n $NAMESPACE   # watch for EXTERNAL-IP"
else
  echo "  kubectl port-forward -n $NAMESPACE svc/linode-instance-scheduler-api 8000:80   # then open http://localhost:8000/ui/"
fi
