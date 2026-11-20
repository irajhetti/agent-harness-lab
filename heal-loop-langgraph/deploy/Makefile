# Local Kubernetes demonstration for heal-loop. Run the targets in this order:
#   make tools lint cluster-up image deploy verify evidence cluster-down
# `evidence` runs verify again, then verify-negative, and saves the output of both.
SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c

# Our own kubeconfig, so ~/.kube/config is never touched. It holds the cluster admin key and is git-ignored.
export KUBECONFIG := $(CURDIR)/.kubeconfig
export KIND_EXPERIMENTAL_PROVIDER := podman
CLUSTER := heal-lab
NS := heal-lab
IMAGE := localhost/heal-loop:0.1.0
K := kubectl -n $(NS)

.PHONY: tools lint cluster-up image deploy verify verify-negative evidence cluster-down

tools: ## install kind, kubectl, kubeconform and kube-linter into ~/.local/bin
	./scripts/tools.sh

lint: ## static checks; no cluster needed
	kubeconform -strict -summary -kubernetes-version 1.37.0 k8s/
	kube-linter lint k8s/

# Rootless podman can give the node its cgroup controllers only if systemd delegates them, hence systemd-run.
# kind has no flag for node size, so the node container is capped afterwards.
cluster-up: ## one single-node kind cluster, capped at 4 CPUs and 4 GB
	systemd-run --user --scope -p Delegate=yes kind create cluster --config kind-config.yaml --kubeconfig $(KUBECONFIG) --wait 180s
	podman update --cpus 4 --memory 4g $(CLUSTER)-control-plane >/dev/null
	kubectl get nodes

# There is no registry. The image goes into the node as a tar archive, streamed through a pipe.
image: ## build the image and load it into the cluster
	podman build -f Containerfile -t $(IMAGE) ..
	podman save $(IMAGE) | kind load image-archive /dev/stdin --name $(CLUSTER)

# The Secret value is random, made here, piped straight into kubectl, and never written to disk or printed.
deploy: ## apply everything, wait for Jaeger, then run both Jobs
	kubectl apply -f k8s/00-namespace.yaml
	openssl rand -hex 24 | $(K) create secret generic heal-loop-demo --from-file=demo-token=/dev/stdin --dry-run=client -o yaml | kubectl apply -f -
	kubectl apply -f k8s/10-config.yaml -f k8s/20-jaeger.yaml -f k8s/40-networkpolicy.yaml -f k8s/50-test.yaml
	$(K) rollout status deployment/lab-jaeger --timeout=240s
	$(K) wait --for=condition=Ready pod/lab-probe pod/lab-blocked --timeout=120s
	$(K) delete job heal-loop-fixed heal-loop-reverted --ignore-not-found # a Job cannot be re-run in place
	kubectl apply -f k8s/30-job-fixed.yaml -f k8s/31-job-reverted.yaml
	$(K) wait --for=condition=Complete job/heal-loop-fixed job/heal-loop-reverted --timeout=240s

verify: ## run every proof; prints PASS or FAIL per check; exits non-zero on any FAIL
	./scripts/verify.sh

# A test that cannot fail proves nothing. This applies a deliberately wrong policy and expects verify to fail.
verify-negative: ## show that verify can fail, then remove the wrong policy again
	kubectl apply -f negative/allow-all-egress.yaml
	if ./scripts/verify.sh; then result="UNEXPECTED: verify passed with a wrong policy"; status=1; \
	else result="EXPECTED: verify failed with the wrong policy applied"; status=0; fi; \
	kubectl delete -f negative/allow-all-egress.yaml; echo "$$result"; exit $$status

evidence: ## save versions, cluster state, Job logs and the verify output into evidence/
	{ date -u +"collected %Y-%m-%dT%H:%M:%SZ"; uname -sr; podman --version; kind version; \
	  kubectl version | grep -v Kustomize; echo "kubeconform $$(kubeconform -v)"; echo "kube-linter $$(kube-linter version)"; \
	  kubectl get nodes -o wide; podman image inspect $(IMAGE) --format 'image $(IMAGE) id {{.Id}} built {{.Created}}'; \
	} > evidence/versions.txt 2>&1
	$(MAKE) --no-print-directory lint > evidence/static-checks.txt 2>&1
	$(K) logs job/heal-loop-fixed > evidence/job-heal-loop-fixed.log
	$(K) logs job/heal-loop-reverted > evidence/job-heal-loop-reverted.log
	./scripts/verify.sh 2>&1 | tee evidence/verify.txt
	$(MAKE) --no-print-directory verify-negative > evidence/verify-negative.txt 2>&1
	./scripts/verify.sh > /dev/null # puts the right policies back and confirms a clean pass
	$(K) get all,networkpolicy -o wide > evidence/get-all.txt 2>&1

# kind also removes the cluster's entry and key from .kubeconfig.
cluster-down: ## delete the cluster
	kind delete cluster --name $(CLUSTER) --kubeconfig $(KUBECONFIG)
