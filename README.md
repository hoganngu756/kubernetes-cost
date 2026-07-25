# Kubernetes Resource and Cost Monitor

Pulls container CPU/memory usage and resource requests out of Prometheus, compares
them, prices the gap against a static AWS-style rate table, and reports which
workloads are wasting the most money.

Built from scratch deliberately — the learning targets are metrics-pipeline design
and cost-calculation logic. Shared/idle cost allocation, spot and reserved-instance
discounts, PV/network costs, and multi-cloud pricing are explicitly out of scope;
OpenCost and Kubecost already solve those and reimplementing them teaches nothing.

## Status

- [x] **Milestone 1** — cluster, monitoring stack, sample workloads, metrics verified
- [ ] **Milestone 2** — PromQL queries + Python metrics puller
- [ ] **Milestone 3** — pricing table, efficiency ratios, waste calculation
- [ ] **Milestone 4** — CLI efficiency report (first full demo)

## Prerequisites

`docker` (running), `kind`, `helm`, `kubectl`, `python3`.

```sh
brew install kind helm
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
```

## Setup

```sh
make up            # cluster + monitoring stack + sample workloads (~5 min)
make port-forward  # Prometheus UI at http://localhost:9090
make status
make down          # tear the whole thing down
```

`make up` is `cluster-up` → `monitoring-up` → `workloads-up`; each is runnable alone.

## Layout

```
cluster/kind-cluster.yaml       3-node kind cluster, node image pinned
cluster/prometheus-values.yaml  minimal kube-prometheus-stack values
workloads/                      sample apps with deliberate misprovisioning
Makefile                        setup/teardown targets, pinned chart version
```

## Sample workloads

Deployed to the `cost-demo` namespace, each engineered to produce a known answer
so the report can be checked against expectations rather than eyeballed:

| Workload | CPU req | Mem req | Actual | Expected verdict |
|---|---|---|---|---|
| `idle-hog` (×2 replicas) | 500m | 512Mi | ~0 CPU, ~0.3Mi | Worst offender on both axes |
| `overprovisioned-web` | 500m | 256Mi | ~100m, ~33Mi | Flagged on both axes |
| `rightsized-worker` | 130m | 64Mi | ~100m, ~49Mi | **Not** flagged — the control case |
| `underprovisioned-cruncher` | 50m | 64Mi | ~200m (throttled), ~16Mi | Flagged on **memory only**; CPU is under-provisioned |

`idle-hog` runs two replicas so the report has to aggregate across pods.
`underprovisioned-cruncher` exists to prove efficiency is computed per-dimension —
recommending a CPU cut there would be actively harmful.

## Pinned versions

| Component | Version | Why pinned |
|---|---|---|
| kind node image | `kindest/node:v1.36.1` | Reproducible cluster |
| kube-prometheus-stack | `87.19.1` | kube-state-metrics label schemas shift between releases, and the cost queries join on those labels |

## Metrics pipeline

Three sources, all provided by the Helm chart:

| Metric | From | Used for |
|---|---|---|
| `kube_pod_container_resource_requests` | kube-state-metrics | What the workload reserved (and therefore is billed for) |
| `container_cpu_usage_seconds_total` | cadvisor | Actual CPU — a counter, needs `rate()` |
| `container_memory_working_set_bytes` | cadvisor | Actual memory — already a gauge |

Requests, not limits, drive the cost model: the scheduler bin-packs nodes on
requests, so requests are what is effectively reserved. Limits are context for
avoiding OOM/throttling in a recommendation, not a cost driver.

### The pod → Deployment join

cadvisor reports per-pod; costs are wanted per-workload. `kube_pod_owner` only
reaches the **ReplicaSet**, so getting to the Deployment takes two hops
(verified working against this cluster):

```promql
sum by (deployment) (
  sum by (pod) (
    rate(container_cpu_usage_seconds_total{namespace="cost-demo",container!=""}[5m])
  )
  * on (pod) group_left(replicaset)
    label_replace(
      kube_pod_owner{namespace="cost-demo", owner_kind="ReplicaSet"},
      "replicaset", "$1", "owner_name", "(.*)"
    )
  * on (replicaset) group_left(deployment)
    label_replace(
      kube_replicaset_owner{namespace="cost-demo", owner_kind="Deployment"},
      "deployment", "$1", "owner_name", "(.*)"
    )
)
```

The `label_replace` calls exist because `on(...)` needs matching label *names* on
both sides, and both metrics call their target `owner_name`.

## Caveat: the p95 window

A disposable kind cluster with synthetic load has no organic usage cycles, so a
p95 over 7 days is meaningless here — the workloads emit flat, near-constant load
by construction. Numbers from this setup are illustrative of the *math*, not of
real production behaviour. The analysis window is therefore short and configurable;
`rate()` needs several minutes of history before it reads accurately at all.
# kubernetes-cost
