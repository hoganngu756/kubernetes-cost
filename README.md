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
- [x] **Milestone 2** — PromQL queries + Python metrics puller (`costmon/metrics.py`)
- [x] **Milestone 3** — pricing table, efficiency ratios, waste calculation (`costmon/pricing.py`, `costmon/cost.py`)
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

| Workload | CPU req | Mem req | Actual (verified) | Verdict |
|---|---|---|---|---|
| `idle-hog` (×2 replicas) | 1000m (2×500m) | 1024Mi (2×512Mi) | 0m, 0.6Mi | Worst offender on both axes (0% efficiency) |
| `overprovisioned-web` | 500m | 256Mi | 156m, 33.1Mi | Flagged on both axes (31% CPU, 13% mem) |
| `rightsized-worker` | 130m | 64Mi | 101m, 48.6Mi | **Not** flagged — control case (78% CPU, 76% mem) |
| `underprovisioned-cruncher` | 50m | 64Mi | 200m (throttled at limit), 16.4Mi | Flagged on **memory only** (26%); CPU is under-provisioned (400%) |

Verified end-to-end via `python3 -m costmon.metrics` against the live cluster, requests
aggregated correctly across `idle-hog`'s 2 replicas.

`idle-hog` runs two replicas so the report has to aggregate across pods.
`underprovisioned-cruncher` exists to prove efficiency is computed per-dimension —
recommending a CPU cut there would be actively harmful.

**Gotcha found while verifying against the live cluster:** the busy-loop workloads
run their "busy" phase for a fixed wall-clock duration, but a CPU *limit* below a
full core throttles how much they actually burn during that window via CFS quota.
Real average usage is `limit × (busy_seconds / cycle_seconds)`, not just "however
long the loop runs" — the first cut of `rightsized-worker` used a duty cycle sized
for the latter and landed at ~30m instead of ~100m, which would have wrongly
flagged the control case. Fixed by solving the duty cycle against the limit
instead of against wall-clock time (see comments in the workload YAML).

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

## Metrics puller (`costmon/`)

```sh
python3 -m costmon.metrics   # prints requests vs. actual usage per workload
```

Stdlib only, no dependencies. `costmon/prometheus.py` is a ~10-line instant-query
wrapper; `costmon/metrics.py` does the pod → Deployment join **in Python**, not as
nested PromQL:

- One query maps pod → ReplicaSet (`kube_pod_owner`), one maps ReplicaSet →
  Deployment (`kube_replicaset_owner`); Python composes the two dicts.
- Requests and usage are pulled as flat per-pod instant queries, then summed
  into per-Deployment totals using that pod → Deployment map.

The PromQL version in this README does the same join in a single nested query
and is kept here as validation, but the join logic lives in Python going
forward — it's one join, unit-testable, and it's the natural place for
Milestone 3's cost math to plug into (`pull_workload_metrics()` returns a plain
`WorkloadMetrics` list, decoupled from any query or output concern).

## Cost calculation (`costmon/pricing.py`, `costmon/cost.py`)

```sh
python3 -m costmon.cost               # ranked waste report against the live cluster
python3 -m unittest discover -v       # hand-calculated math checks, no cluster needed
```

`costmon/pricing.py` is a static, blended AWS-style rate ($/vCPU-hr, $/GiB-hr
derived from m5.xlarge on-demand pricing) -- see the module docstring for why a
flat rate, not per-instance-type pricing, is the right amount of precision here.

`costmon/cost.py` takes `WorkloadMetrics` and, independently per CPU and memory:
computes `efficiency = usage / request`, flags "overprovisioned" below 40%
efficiency, and recommends `usage * 1.3` as the new request when flagged (left
untouched otherwise -- an under-provisioned workload never gets a recommended
cut). Waste $ is priced only on the flagged axis.

The unit tests in `tests/test_cost.py` check the $ math against hand-calculated
values and cover what the live cluster can't: a workload under-provisioned on
CPU while over-provisioned on memory at the same time, and the zero-request
edge case. This is also why efficiency is computed per-dimension rather than as
one blended score -- a single combined ratio would hide exactly that case.

Verified live cluster output (waste-ranked):

| Workload | CPU eff | Mem eff | $/mo cost | $/mo waste |
|---|---|---|---|---|
| `idle-hog` | 0% | 1% | 43.80 | 43.74 |
| `overprovisioned-web` | 32% | 13% | 19.71 | 12.07 |
| `underprovisioned-cruncher` | 398% | 26% | 2.30 | 0.36 |
| `rightsized-worker` | 77% | 76% | 5.10 | 0.00 |

## Caveat: the p95 window

A disposable kind cluster with synthetic load has no organic usage cycles, so a
p95 over 7 days is meaningless here — the workloads emit flat, near-constant load
by construction. Numbers from this setup are illustrative of the *math*, not of
real production behaviour. The analysis window is therefore short and configurable;
`rate()` needs several minutes of history before it reads accurately at all.
# kubernetes-cost
