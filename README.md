# costmon

Finds Kubernetes workloads that request more CPU and memory than they use, and
estimates what that costs per month. It reads usage and requests from
Prometheus, compares them per workload, and suggests smaller requests. It
runs as a CLI report or as an MCP server for LLM agents.

Pure Python standard library, no dependencies.

## Requirements

- Python 3.10+
- For the local demo cluster: `docker`, `kind`, `helm`, `kubectl`

## Install

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

This puts `costmon` and `costmon-mcp` on the venv's PATH.

## Quick start

```sh
brew install kind helm
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts

make up             # kind cluster, kube-prometheus-stack, demo workloads (~5 min)
make port-forward   # keep running in another shell; exposes Prometheus on :9090
costmon
make down           # delete the cluster
```

Wait about 15 minutes after `make up` so Prometheus has a full window of data.
If `make up` fails with `node(s) already exist`, run `make down` first.

## Usage

```sh
costmon [--prometheus-url URL] [--namespace NS] [--window 15m]
        [--threshold 0.4] [--no-chart]
```

| Flag | Default | |
|---|---|---|
| `--prometheus-url` | `http://localhost:9090` | Prometheus HTTP API |
| `--namespace` | `cost-demo` | Namespace to analyse |
| `--window` | `15m` | How far back to look at usage (any PromQL duration) |
| `--threshold` | `0.4` | Flag an axis when usage / request is below this |
| `--chart` / `--no-chart` | on | Request-vs-usage bar chart |

Live output from the demo fleet, 15m window (chart omitted):

```
workload                                cpu eff  mem eff   $/mo cost  $/mo waste
deployment/idle-hog                          0%       0%       25.84       25.29
deployment/overprovisioned-web              32%      15%       12.15        7.20
deployment/underprovisioned-cruncher       400%      26%        1.33        0.13
deployment/api-gateway                      76%      70%       16.11        0.00
...
TOTAL                                                         112.38       32.62

Over-provisioned: 3 of 10 workloads (30%)
Recommended request changes per pod (efficiency < 40%, 1.3x headroom):
  deployment/idle-hog                   (2 pods)   cpu 500m -> 10m         mem 512Mi -> 16Mi
  deployment/overprovisioned-web        (1 pod)    cpu 500m -> 211m        mem 256Mi -> 51Mi
  deployment/underprovisioned-cruncher  (1 pod)    cpu ok                  mem 64Mi -> 21Mi
```

Cost and waste are workload totals. Recommendations are per pod, so they can
be copied into the manifest. For a pod with several containers, split the
value across them.

## How it works

**Usage.** CPU is the p95 of the per-container CPU rate over the window. Memory
is the peak working set over the window. Both are taken per container
(collapsing duplicate series, e.g. from a restart, to their max) and summed
per workload. A
peak-aware number is used instead of an average because an average-based
request would be exceeded half the time.

**Workloads.** Each running pod is attributed to its owner: a Deployment
(through its ReplicaSet), StatefulSet, DaemonSet, CronJob (through its Job),
standalone Job, or the pod itself if it has no owner. Pods that aren't
Running (Pending, Failed, Evicted) are skipped, since they reserve nothing.

**Efficiency.** `usage / request`, calculated separately for CPU and memory.
A workload can be flagged on one axis and not the other.
`underprovisioned-cruncher` uses 4x its CPU request and a quarter of its
memory request, so only memory gets a recommendation.

**Recommendation.** For a flagged axis: `usage × 1.3`, with a minimum of 10m
CPU / 16Mi memory per pod, and never more than the current request.

**Cost.** Priced on requests, not usage, since requests are what the scheduler
reserves. Rates come from an m5.xlarge (us-east-1 on-demand, $0.192/hr, 4 vCPU
/ 16 GiB), split 65% CPU / 35% memory:

| | Rate |
|---|---|
| CPU | $0.0312 per vCPU-hour |
| Memory | $0.0042 per GiB-hour |
| Month | 730 hours |

Waste is the monthly cost of `request - recommendation` on flagged axes only.
A workload above the threshold shows $0 waste even if it has some slack.

**Limitations**

- One namespace at a time.
- Pricing is a fixed snapshot, not per node type or region.
- No auth support for Prometheus.

## Demo fleet

`workloads/` holds 10 Deployments (40 pods) in `cost-demo`. Each one runs a
busybox loop tuned to a known CPU and memory usage (the math is in each
manifest's header comment), so the report has a known correct answer.

| Workload | Pods | Requests per pod | Expected result |
|---|---|---|---|
| `idle-hog` | 2 | 500m / 512Mi | Flagged on CPU and memory; does nothing |
| `overprovisioned-web` | 1 | 500m / 256Mi | Flagged on CPU and memory |
| `underprovisioned-cruncher` | 1 | 50m / 64Mi | Flagged on memory only; needs more CPU |
| `api-gateway` | 8 | 80m / 64Mi | Not flagged |
| `event-consumer` | 6 | 100m / 64Mi | Not flagged |
| `session-cache` | 6 | 32m / 128Mi | Not flagged |
| `search-indexer` | 5 | 100m / 128Mi | Not flagged |
| `notification-worker` | 5 | 80m / 64Mi | Not flagged (closest to the threshold) |
| `metrics-forwarder` | 5 | 70m / 64Mi | Not flagged |
| `rightsized-worker` | 1 | 130m / 64Mi | Not flagged |

The seven unflagged workloads are the control group: if one shows up in the
recommendations, something is wrong. The fleet uses about 2.3 cores and 2 GiB
while running.

## MCP server

```sh
claude mcp add costmon -- /path/to/repo/.venv/bin/costmon-mcp
```

Or in a client config:

```json
{"mcpServers": {"costmon": {"command": "/path/to/repo/.venv/bin/costmon-mcp"}}}
```

Use the full path to the venv's `costmon-mcp`, since the client won't have
the venv activated.

| Tool | Returns |
|---|---|
| `list_workloads` | Requests and usage per workload |
| `get_cost_report` | Efficiency, monthly cost and waste per workload, ranked by waste, with totals |
| `get_rightsizing_recommendations` | Current vs. recommended requests per pod for flagged workloads |

All arguments (`namespace`, `window`, `threshold`, `prometheus_url`) are
optional. Server-wide defaults can be set with the same flags as the CLI
(`--prometheus-url`, `--namespace`, `--window`).

The server speaks JSON-RPC over stdio directly rather than through the MCP SDK.
It still needs `make port-forward` running; if Prometheus is unreachable or
rejects a query, the tool call returns an error and the server keeps running.

## Layout

```
cluster/                kind and kube-prometheus-stack config (versions pinned)
workloads/              demo Deployments
costmon/prometheus.py   Prometheus HTTP client
costmon/metrics.py      PromQL queries, pod -> workload join
costmon/pricing.py      rates
costmon/cost.py         efficiency, recommendations, waste
costmon/cli.py          report
costmon/mcp_server.py   MCP server
tests/                  unit tests, no cluster needed
```

## Tests

```sh
python3 -m unittest discover -v
```

No cluster needed. Cost and waste are checked against hand-calculated values,
the MCP server is tested by sending JSON-RPC through its stdio loop, and
`test_fleet.py` parses `workloads/*.yaml` to confirm the demo fleet still
produces the results in the table above.
