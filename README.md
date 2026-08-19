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
- [x] **Milestone 4** — peak-aware usage statistics + CLI report with request-vs-usage
  delta bars, measured against a 40-pod fleet (`costmon/cli.py`)
- [x] **Milestone 5** — the pipeline exposed to LLM agents as an MCP server (`costmon/mcp_server.py`)

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
`make mcp` runs the MCP server; `python3 -m costmon.cli` prints the report. Both need
`make port-forward` up in another shell.

## Layout

```
cluster/kind-cluster.yaml       3-node kind cluster, node image pinned
cluster/prometheus-values.yaml  minimal kube-prometheus-stack values
workloads/                      10 Deployments / 40 pods, 3 deliberately misprovisioned
Makefile                        setup/teardown targets, pinned chart version
costmon/prometheus.py           Prometheus HTTP API wrapper (stdlib only)
costmon/metrics.py              pod -> Deployment join, requests vs. usage
costmon/pricing.py              static blended $/vCPU-hr and $/GiB-hr
costmon/cost.py                 efficiency, recommendations, waste $
costmon/cli.py                  the report -- CLI entry point
costmon/mcp_server.py           the same pipeline as MCP tools for agents
tests/                          math, rendering and MCP protocol checks, no cluster needed
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
| `idle-hog` (×2 replicas) | 1000m (2×500m) | 1024Mi (2×512Mi) | 0m, 0.6Mi | Worst offender on both axes (0% / 0%) |
| `overprovisioned-web` | 500m | 256Mi | 173m, 43Mi | Flagged on both axes (35% CPU, 17% mem) |
| `underprovisioned-cruncher` | 50m | 64Mi | 200m (throttled at limit), 16Mi | Flagged on **memory only** (26%); CPU is under-provisioned (400%) |

### The 7 honestly-sized workloads (control group)

Sized off observed usage, so none should be flagged. If any of these appears in
the recommendations block, the threshold or the math is wrong.

Predicted from the duty-cycle model, then measured against the live cluster
(15m window):

| Workload | Replicas | CPU req | Mem req | CPU eff pred → meas | Mem eff pred → meas |
|---|---|---|---|---|---|
| `api-gateway` | 8 | 80m | 64Mi | 75% → **77%** | 65% → **71%** |
| `session-cache` | 6 | 32m | 128Mi | 62% → **66%** | 76% → **78%** |
| `event-consumer` | 6 | 100m | 64Mi | 67% → **69%** | 77% → **86%** |
| `search-indexer` | 5 | 100m | 128Mi | 75% → **77%** | 64% → **68%** |
| `notification-worker` | 5 | 80m | 64Mi | 63% → **64%** | 52% → **57%** |
| `metrics-forwarder` | 5 | 70m | 64Mi | 71% → **73%** | 65% → **70%** |
| `rightsized-worker` | 1 | 130m | 64Mi | 78% → **80%** | 76% → **87%** |

**CPU predictions held within 1–4 points** — the duty-cycle model
(`usage = cpu_limit × busy/cycle`) is accurate at fleet scale.

**Memory came in 2–11 points high**, consistently in the same direction. The
prediction assumed working set ≈ ballast size, but `max_over_time` catches the
*peak*, which includes transient allocation while `dd` writes the ballast file.
The error is one-sided and safe — it pushes efficiency up, away from the
threshold — but a memory prediction here is a lower bound, not an estimate.

`notification-worker` sits closest to the 40% threshold (57% memory measured) on
purpose — it is the workload that would break first if the threshold logic
regressed, and 57% still leaves real margin rather than a lucky pass.

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

The PromQL version in this README does the same join in a single nested query and
is kept as validation of the **join** only — it still shows a plain `rate()`,
whereas the shipped queries use the peak-aware statistics described below. The
join logic lives in Python going forward: it's one join, unit-testable, and the
natural seam for the cost math to plug into (`pull_workload_metrics()` returns a
plain `WorkloadMetrics` list, decoupled from any query or output concern).

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

Measured against the live 40-pod cluster, 15m window, p95-CPU / max-memory
statistic (the full report is below):

| Workload | CPU eff | Mem eff | $/mo cost | $/mo waste |
|---|---|---|---|---|
| `idle-hog` | 0% | 0% | 43.80 | 43.79 |
| `overprovisioned-web` | 35% | 17% | 19.71 | 11.35 |
| `underprovisioned-cruncher` | 400% | 26% | 2.30 | 0.37 |
| 7 honestly-sized workloads | 64–80% | 57–87% | 124.27 | 0.00 |

The CPU predictions in the workload tables above landed within 1–4 points; memory
ran 2–11 points high for the reason given there. Both misses are one-sided and
away from the threshold, so no control workload came close to being flagged.

## CLI (`costmon/cli.py`)

```sh
make port-forward                     # in another shell; the CLI reads Prometheus over it
python3 -m costmon.cli                # waste-ranked report, delta bars, recommendations
python3 -m costmon.cli --help         # --namespace --window --threshold --no-chart
python3 -m unittest discover -v       # hand-calculated math checks, no cluster needed
```

Real output, 40-pod cluster, 15m window:

```
workload                    cpu eff  mem eff   $/mo cost  $/mo waste
idle-hog                         0%       0%       43.80       43.79
overprovisioned-web             35%      17%       19.71       11.35
underprovisioned-cruncher      400%      26%        2.30        0.37
api-gateway                     77%      71%       26.81        0.00
event-consumer                  69%      86%       24.31        0.00
metrics-forwarder               73%      70%       15.00        0.00
notification-worker             64%      57%       16.75        0.00
rightsized-worker               80%      87%        5.10        0.00
search-indexer                  77%      68%       23.00        0.00
session-cache                   66%      78%       13.30        0.00
TOTAL                                             190.07       55.51

Request vs. usage  (█ used  ░ idle headroom  ▓ over request)

  CPU
    idle-hog                  ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░   1000m req       0m used
    overprovisioned-web       ██████░░░░░░░░░░░                     500m req     173m used
    underprovisioned-cruncher ██▓▓▓▓▓                                50m req     200m used
    api-gateway               █████████████████░░░░░                640m req     491m used
    event-consumer            ██████████████░░░░░░                  600m req     413m used
    metrics-forwarder         █████████░░░                          350m req     257m used
    notification-worker       █████████░░░░░                        400m req     257m used
    rightsized-worker         ████                                  130m req     104m used
    search-indexer            █████████████░░░░                     500m req     387m used
    session-cache             ████░░░                               192m req     127m used

  Memory
    idle-hog                  ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  1024Mi req      1Mi used
    overprovisioned-web       █░░░░░░░                             256Mi req     43Mi used
    underprovisioned-cruncher █░                                    64Mi req     16Mi used
    api-gateway               ████████████░░░░░                    512Mi req    366Mi used
    event-consumer            ███████████░░                        384Mi req    329Mi used
    metrics-forwarder         ███████░░░░                          320Mi req    223Mi used
    notification-worker       ██████░░░░░                          320Mi req    182Mi used
    rightsized-worker         ██                                    64Mi req     56Mi used
    search-indexer            ███████████████░░░░░░                640Mi req    437Mi used
    session-cache             ████████████████████░░░░░░           768Mi req    600Mi used

Over-provisioned: 3 of 10 workloads (30%)
Recommended request changes (efficiency < 40%, 1.3x headroom):
  idle-hog                  cpu 1000m -> 0m           mem 1024Mi -> 1Mi
  overprovisioned-web       cpu 500m -> 225m          mem 256Mi -> 56Mi
  underprovisioned-cruncher cpu ok                    mem 64Mi -> 21Mi
```

Bar length is proportional to the *request*, so a row's width shows what the
workload costs and the unfilled tail shows what it wastes -- `idle-hog` at 0%
efficiency reads as far worse than `underprovisioned-cruncher`, which is also
badly sized but 20x smaller. `▓` marks usage running past the request.
`--no-chart` suppresses the bars.

The recommendations block is the actionable half: `evaluate()` was already
computing recommended requests in Milestone 3, but nothing printed them. Only
the flagged axis gets a recommendation — `cpu ok` on `underprovisioned-cruncher`
is the per-dimension logic showing its work.

**Known rough edge:** a workload using nothing at all renders as `cpu 1000m -> 0m`,
which is not a request anyone can apply. The math is right (usage really is zero);
the useful advice there is "delete this workload", and no minimum-request floor
has been decided on yet.

## MCP server (`costmon/mcp_server.py`)

The same pipeline exposed to LLM agents as MCP tools, so an agent can ask about
utilization and cost instead of parsing CLI output.

```sh
make port-forward                        # required -- the server reads Prometheus over it
make mcp                                 # run the server on stdio (clients normally launch it)
claude mcp add costmon -- python3 -m costmon.mcp_server
```

Speaks MCP over stdio as newline-delimited JSON-RPC 2.0, hand-rolled rather than
via the MCP SDK to keep the project's zero-dependency property -- the surface an
agent needs is three tools and four protocol methods (`initialize`, `tools/list`,
`tools/call`, `ping`), which is less code than taking on a dependency.

| Tool | Answers |
|---|---|
| `list_workloads` | "what is this namespace using?" -- requests vs. usage per Deployment |
| `get_cost_report` | "what is it costing?" -- efficiency, $/mo cost and waste, ranked, plus fleet totals and over-provisioned share |
| `get_rightsizing_recommendations` | "what should I change?" -- flagged workloads only, current → recommended |

Three tools rather than one with a mode argument: agents choose better from
distinct names and descriptions. Every argument (`namespace`, `window`,
`threshold`, `prometheus_url`) is optional with a server-side default, so a tool
call with `{}` still returns a real answer.

`get_rightsizing_recommendations` omits an axis entirely when it must not be cut,
rather than reporting it as "no change":

```json
{ "workload": "underprovisioned-cruncher",
  "monthly_waste_usd": 0.37,
  "memory": { "current_request_mib": 64.0, "recommended_request_mib": 21.3 } }
```

No `cpu` key, because that workload needs *more* CPU. The per-dimension logic
matters more here than in the CLI: a human reading `cpu ok` understands it, but
an agent handed a `cpu` field could act on it.

**Operational caveat:** an MCP client may launch this server at any time, but the
pipeline only works while `make port-forward` is running. An unreachable
Prometheus therefore comes back as a *tool* error carrying that hint
(`isError: true`), not a protocol error -- the agent can read it and retry
instead of the connection dying.

## Testing

```sh
python3 -m unittest discover -v      # 24 tests, no cluster required
```

| File | Covers |
|---|---|
| `tests/test_cost.py` | waste $ against hand-calculated values, per-dimension flagging, `--threshold` override, zero-request edge case |
| `tests/test_cli.py` | TOTAL row sums the rows, only the flagged axis is recommended, delta-bar geometry (fill ratio, overflow, zero-scale guard) |
| `tests/test_mcp_server.py` | real JSON-RPC frames through `serve()`: handshake and version fallback, notifications getting no reply, parse/method errors, unreachable Prometheus as a tool error, per-tool payloads |

Nothing here needs a cluster. That is deliberate: the fabricated inputs cover the
cases the live cluster *cannot* produce — a workload under-provisioned on CPU and
over-provisioned on memory simultaneously, a zero request, a refused Prometheus
connection — while the cluster covers the one thing unit tests can't, which is
whether the PromQL and the pod → Deployment join are right at all.

The MCP tests deliberately drive the stdio loop rather than calling the tool
methods directly: the claim is "this is a working MCP server", and the protocol
framing is the part of that most likely to break.

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
so p95 ≈ mean ≈ max here and the percentile does nothing visible. This was confirmed
directly: a 4m window and a 15m window produced the same verdicts and efficiencies
within a few points, which is exactly what flat load predicts and exactly why this
cluster cannot demonstrate the statistic's value. Numbers here are illustrative of
the *math*, not of real production behaviour.
