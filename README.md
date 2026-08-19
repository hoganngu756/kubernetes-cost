# Kubernetes Resource and Cost Monitor

Pulls container CPU/memory usage and resource requests out of Prometheus, prices
the gap against a static AWS-style rate table, and reports which workloads are
wasting the most money — as a CLI report and as MCP tools for LLM agents.

Built from scratch deliberately: the learning targets are metrics-pipeline design
and cost-calculation logic. Reasoning behind the design choices is in
[DESIGN.md](DESIGN.md).

## Status

- [x] **M1** — cluster, monitoring stack, sample workloads
- [x] **M2** — PromQL queries + Python metrics puller (`costmon/metrics.py`)
- [x] **M3** — pricing table, efficiency ratios, waste calculation (`costmon/cost.py`)
- [x] **M4** — peak-aware statistics + CLI report with delta bars (`costmon/cli.py`)
- [x] **M5** — the pipeline exposed to LLM agents as MCP tools (`costmon/mcp_server.py`)

## Quick start

Needs `docker` (running), `kind`, `helm`, `kubectl`, `python3`.

```sh
brew install kind helm
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts

make up               # cluster + monitoring + workloads (~5 min)
make port-forward     # in another shell -- everything below reads Prometheus over it
python3 -m costmon.cli
make down             # tear it all down
```

`make up` is `cluster-up` → `monitoring-up` → `workloads-up`; each runs alone.
If it fails with `node(s) already exist`, run `make down` first.

## Layout

```
cluster/                 kind + kube-prometheus-stack config, versions pinned
workloads/               10 Deployments / 40 pods, 3 deliberately misprovisioned
costmon/prometheus.py    Prometheus HTTP API wrapper (stdlib only)
costmon/metrics.py       pod -> Deployment join, requests vs. usage
costmon/pricing.py       static blended $/vCPU-hr and $/GiB-hr
costmon/cost.py          efficiency, recommendations, waste $
costmon/cli.py           the report -- CLI entry point
costmon/mcp_server.py    the same pipeline as MCP tools for agents
tests/                   math, rendering and protocol checks, no cluster needed
```

## The demo fleet

10 Deployments / 40 pods in the `cost-demo` namespace, of which **3 (30%) are
deliberately misprovisioned**. Every workload is engineered to produce a known
answer, so the report can be checked against expectations rather than eyeballed.

These numbers are *by construction, not a discovery* — this is a synthetic kind
cluster whose purpose is to have a known right answer. It demonstrates the math
on a fleet-sized input; it is not a finding about anyone's production cluster.

| Workload | Pods | CPU req | Mem req | CPU eff | Mem eff | Verdict |
|---|---|---|---|---|---|---|
| `idle-hog` | 2 | 1000m | 1024Mi | 0% | 0% | **Flagged** — reserves both, uses neither |
| `overprovisioned-web` | 1 | 500m | 256Mi | 35% | 17% | **Flagged** on both axes |
| `underprovisioned-cruncher` | 1 | 50m | 64Mi | 400% | 26% | **Flagged on memory only** — needs *more* CPU |
| `api-gateway` | 8 | 640m | 512Mi | 77% | 71% | ok |
| `event-consumer` | 6 | 600m | 384Mi | 69% | 86% | ok |
| `session-cache` | 6 | 192m | 768Mi | 66% | 78% | ok |
| `search-indexer` | 5 | 500m | 640Mi | 77% | 68% | ok |
| `notification-worker` | 5 | 400m | 320Mi | 64% | 57% | ok |
| `metrics-forwarder` | 5 | 350m | 320Mi | 73% | 70% | ok |
| `rightsized-worker` | 1 | 130m | 64Mi | 80% | 87% | ok |

Efficiencies measured live at a 15m window. The seven honestly-sized workloads
are the control group: if any appears in the recommendations, the math is wrong.
`notification-worker` sits closest to the threshold (57%) on purpose.
`underprovisioned-cruncher` proves efficiency is computed per-dimension.

**Running cost:** the fleet burns ~2.3 cores and ~2 GiB while up.

## The report

```sh
python3 -m costmon.cli --help     # --namespace --window --threshold --no-chart
```

Real output, 40-pod cluster, 15m window (bars abbreviated here):

```
workload                    cpu eff  mem eff   $/mo cost  $/mo waste
idle-hog                         0%       0%       43.80       43.79
overprovisioned-web             35%      17%       19.71       11.35
underprovisioned-cruncher      400%      26%        2.30        0.37
api-gateway                     77%      71%       26.81        0.00
...
TOTAL                                             190.07       55.51

Request vs. usage  (█ used  ░ idle headroom  ▓ over request)

  CPU
    idle-hog                  ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░   1000m req       0m used
    overprovisioned-web       ██████░░░░░░░░░░░                     500m req     173m used
    underprovisioned-cruncher ██▓▓▓▓▓                                50m req     200m used
    api-gateway               █████████████████░░░░░                640m req     491m used

Over-provisioned: 3 of 10 workloads (30%)
Recommended request changes (efficiency < 40%, 1.3x headroom):
  idle-hog                  cpu 1000m -> 0m           mem 1024Mi -> 1Mi
  overprovisioned-web       cpu 500m -> 225m          mem 256Mi -> 56Mi
  underprovisioned-cruncher cpu ok                    mem 64Mi -> 21Mi
```

Bar length is proportional to the *request*, so a row's width shows what the
workload costs and the unfilled tail shows what it wastes — `idle-hog` reads as
far worse than `underprovisioned-cruncher`, which is also badly sized but 20x
smaller. `▓` marks usage running past the request.

Only the flagged axis gets a recommendation: `cpu ok` on
`underprovisioned-cruncher` is the per-dimension logic showing its work.

## MCP server

The same pipeline as MCP tools, so an agent can ask about utilization and cost
instead of parsing CLI output.

```sh
claude mcp add costmon -- python3 -m costmon.mcp_server
make mcp        # or run it directly on stdio
```

| Tool | Answers |
|---|---|
| `list_workloads` | "what is this namespace using?" |
| `get_cost_report` | "what is it costing?" — efficiency, cost, waste, ranked, plus totals |
| `get_rightsizing_recommendations` | "what should I change?" — flagged workloads only |

Speaks MCP over stdio as newline-delimited JSON-RPC 2.0, hand-rolled rather than
via the SDK to keep the project dependency-free. Every argument (`namespace`,
`window`, `threshold`, `prometheus_url`) is optional with a server-side default,
so a call with `{}` still returns a real answer.

**Caveat:** a client may launch the server at any time, but the pipeline only
works while `make port-forward` is running. An unreachable Prometheus comes back
as a *tool* error carrying that hint (`isError: true`), not a protocol error, so
the agent can read it and retry.

## Testing

```sh
python3 -m unittest discover -v     # 24 tests, no cluster required
```

`test_cost.py` checks waste $ against hand-calculated values; `test_cli.py`
covers table totals and delta-bar geometry; `test_mcp_server.py` drives real
JSON-RPC frames through `serve()`. The fabricated inputs cover what the live
cluster can't produce — simultaneous under-CPU/over-memory, zero requests, a
refused connection.
