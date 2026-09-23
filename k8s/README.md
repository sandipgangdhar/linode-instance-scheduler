# Kubernetes / LKE deployment

Manifests and the orchestration script for running this tool on Linode Kubernetes Engine
(LKE), as an alternative to the VM deployment in `DEPLOYMENT.md`.

**Start with [`DEPLOYMENT-LKE.md`](../DEPLOYMENT-LKE.md)** for the full
walkthrough — this directory is the manifests it references, not a standalone entry point.

| File | What it is |
|---|---|
| `deploy-lke.sh` | The orchestration script — detects an existing `Linode-LKE-Private-Network` VLAN-gating deployment on your cluster, asks whether to run under it, and applies everything below in order. Run this, not `kubectl apply -f` on individual files, unless you know you want to. |
| `namespace.yaml` | The dedicated `linode-instance-scheduler` namespace. |
| `configmap.example.yaml` | Non-secret tunables (poll interval/window, CORS origins). Copy to `configmap.yaml`. |
| `secret.example.yaml` | `LINODE_API_TOKEN` and the other credentials from `.env.example`. Copy to `secret.yaml` (gitignored). |
| `ssh-key-secret.example.yaml` | The deployment's own SSH private key, mounted as a file. Copy to `ssh-key-secret.yaml` (gitignored). |
| `pvc.yaml` | Persistent storage for the SQLite registry and lock files. |
| `deployment.yaml` | The Deployment itself — one pod, two containers (`poll` + `serve-api`), pinned to exactly one replica. Read its own header comment before changing `replicas` or splitting the containers apart — both are load-bearing. |
| `service.clusterip.yaml` | Default exposure for the API/dashboard: in-cluster only. |
| `service.loadbalancer.yaml` | Opt-in public exposure via a real Linode NodeBalancer. |

The container image itself is built from the repo root — see the top-level `Dockerfile`.
