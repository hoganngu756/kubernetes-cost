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
- [x] **Milestone 4** — peak-aware usage statistics + CLI efficiency report (`costmon/cli.py`)

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
costmon/prometheus.py           Prometheus HTTP API wrapper (stdlib only)
costmon/metrics.py              pod -> Deployment join, requests vs. usage
costmon/pricing.py              static blended $/vCPU-hr and $/GiB-hr
costmon/cost.py                 efficiency, recommendations, waste $
costmon/cli.py                  the report -- entry point
tests/                          math and rendering checks, no cluster needed
```

## Sample workloads

Deployed to the `cost-demo` namespace: **10 Deployments / 40 pods**, of which
**3 (30%) are deliberately misprovisioned**. Every workload is engineered to
produce a known answer, so the report can be checked against expectations
rather than eyeballed.

These numbers are *by construction, not a discovery* — this is a synthetic kind
cluster whose whole purpose is to have a known right answer. It demonstrates the
math on a fleet-sized input; it is not a finding about anyone's production cluster.

### The 3 misprovisioned workloads

| Workload | CPU req | Mem req | Actual (verified) | Verdict |
|---|---|---|---|---|
| `idle-hog` (×2 replicas) | 1000m (2×500m) | 1024Mi (2×512Mi) | 0m, 0.6Mi | Worst offender on both axes (0% efficiency) |
| `overprovisioned-web` | 500m | 256Mi | 156m, 33.1Mi | Flagged on both axes (31% CPU, 13% mem) |
| `underprovisioned-cruncher` | 50m | 64Mi | 200m (throttled at limit), 16.4Mi | Flagged on **memory only** (26%); CPU is under-provisioned (400%) |

### The 7 honestly-sized workloads (control group)

Sized off observed usage, so none should be flagged. If any of these appears in
the recommendations block, the threshold or the math is wrong.

| Workload | Replicas | CPU req | Mem req | Expected CPU eff | Expected mem eff |
|---|---|---|---|---|---|
| `api-gateway` | 8 | 80m | 64Mi | ~75% | ~65% |
| `session-cache` | 6 | 32m | 128Mi | ~62% | ~76% |
| `event-consumer` | 6 | 100m | 64Mi | ~67% | ~77% |
| `search-indexer` | 5 | 100m | 128Mi | ~75% | ~64% |
| `notification-worker` | 5 | 80m | 64Mi | ~63% | ~52% |
| `metrics-forwarder` | 5 | 70m | 64Mi | ~71% | ~65% |
| `rightsized-worker` | 1 | 130m | 64Mi | ~78% | ~76% |

`notification-worker` sits closest to the 40% threshold (~52% memory) on purpose —
it is the workload that would break first if the threshold logic regressed.

`idle-hog` runs two replicas so the report has to aggregate across pods.
`underprovisioned-cruncher` exists to prove efficiency is computed per-dimension —
recommending a CPU cut there would be actively harmful.

**Running cost:** the full fleet burns roughly 2.3 cores and ~2 GiB while up.
`make down` ends it.

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
| `container_memory_working_set_bytes` | cadvisor | Actual memory — already a gauge, but read as `max_over_time` (see below) |

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

## Metrics puller (`costmon/metrics.py`)

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

Live cluster output from Milestone 3 (waste-ranked), measured with the older
average-CPU / instant-memory statistic:

| Workload | CPU eff | Mem eff | $/mo cost | $/mo waste |
|---|---|---|---|---|
| `idle-hog` | 0% | 1% | 43.80 | 43.74 |
| `overprovisioned-web` | 32% | 13% | 19.71 | 12.07 |
| `underprovisioned-cruncher` | 398% | 26% | 2.30 | 0.36 |
| `rightsized-worker` | 77% | 76% | 5.10 | 0.00 |

Milestone 4 changed the usage statistic (see below). Because every workload here
emits flat load by construction, p95 and max should land within a percent or two
of these figures — but that has **not** been re-measured against a live cluster yet.

## CLI (`costmon/cli.py`)

```sh
make port-forward                     # in another shell; the CLI reads Prometheus over it
python3 -m costmon.cli                # waste-ranked report + recommended request changes
python3 -m costmon.cli --help         # --namespace, --window, --threshold, --prometheus-url
python3 -m unittest discover -v       # hand-calculated math checks, no cluster needed
```

```
workload                    cpu eff  mem eff   $/mo cost  $/mo waste
idle-hog                         0%       0%       43.80       43.79
overprovisioned-web             31%      13%       19.71       12.24
underprovisioned-cruncher      400%      26%        2.30        0.37
rightsized-worker               78%      76%        5.10        0.00
TOTAL                                              70.91       56.39

Recommended request changes (efficiency < 40%, 1.3x headroom):
  idle-hog                  cpu 1000m -> 0m           mem 1024Mi -> 1Mi
  overprovisioned-web       cpu 500m -> 203m          mem 256Mi -> 43Mi
  underprovisioned-cruncher cpu ok                    mem 64Mi -> 21Mi
```

(Rendered from the expected workload figures, not a live run — cluster was down.)

The recommendations block is the actionable half: `evaluate()` was already
computing recommended requests in Milestone 3, but nothing printed them. Only
the flagged axis gets a recommendation — `cpu ok` on `underprovisioned-cruncher`
is the per-dimension logic showing its work.

**Known rough edge:** a workload using nothing at all renders as `cpu 1000m -> 0m`,
which is not a request anyone can apply. The math is right (usage really is zero);
the useful advice there is "delete this workload", and no minimum-request floor
has been decided on yet.

## The usage statistic: p95 CPU, max memory

Requests are sized off a peak-aware statistic, not an average, because these
numbers feed recommendations that get applied:

| Dimension | Statistic | Why |
|---|---|---|
| CPU | `quantile_over_time(0.95, rate(...)[2m])` | CPU is compressible — exceeding the request costs latency, not a kill. Sizing on max would reserve every workload's worst second. |
| Memory | `max_over_time(...)` | Memory is not compressible. Exceeding the request risks eviction/OOMKill, so the peak is the only safe basis. |

Both are computed **per container** and then summed, matching how requests are
summed — a request is set per container, so that's the unit a recommendation
applies to.

The inner `rate()` window is 2m: at the pinned 30s `scrapeInterval` that gives
four samples, and `rate()` needs several before it reads accurately at all. The
outer window (`--window`, default 15m) is sampled at a 1m step, so the default
takes a percentile over ~15 points.

**Caveat this doesn't fix:** a disposable kind cluster with synthetic load has no
organic usage cycles — the workloads emit flat, near-constant load by construction,
so p95 ≈ mean ≈ max here and the percentile does nothing visible. Numbers from this
setup are illustrative of the *math*, not of real production behaviour. The
statistic is correct; this cluster just can't demonstrate why it matters.
