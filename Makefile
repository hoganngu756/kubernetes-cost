CLUSTER_NAME  ?= kubecost-lab
MONITORING_NS ?= monitoring
# Pinned: kube-state-metrics label schemas shift between chart releases, and the
# cost queries join on those labels. Bump deliberately, not incidentally.
CHART_VERSION ?= 87.19.1
RELEASE       ?= kube-prometheus-stack

.PHONY: up down cluster-up monitoring-up workloads-up port-forward status

up: cluster-up monitoring-up workloads-up

cluster-up:
	kind create cluster --config cluster/kind-cluster.yaml

monitoring-up:
	helm upgrade --install $(RELEASE) prometheus-community/kube-prometheus-stack \
		--version $(CHART_VERSION) \
		--namespace $(MONITORING_NS) --create-namespace \
		--values cluster/prometheus-values.yaml \
		--wait --timeout 10m

workloads-up:
	kubectl apply -f workloads/

# Prometheus UI at http://localhost:9090
port-forward:
	kubectl -n $(MONITORING_NS) port-forward svc/$(RELEASE)-prometheus 9090:9090

status:
	kubectl get pods -n $(MONITORING_NS)
	kubectl get pods -n cost-demo

down:
	kind delete cluster --name $(CLUSTER_NAME)
