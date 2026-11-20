# heal-loop on a local Kubernetes cluster

This folder packages the heal loop as a container image and runs it on a one-node Kubernetes
cluster on a single machine. It shows how an agent workload can be confined on Kubernetes, and it
tests each confinement claim against the running cluster.

Built with AI coding agents under the owner's direction as a demonstration project.

It does not change the application. Nothing outside `deploy/` is edited.

## What was run

| Part | Version |
|---|---|
| Cluster | kind v0.33.0, one node, Kubernetes v1.37.0, node image pinned by digest |
| Container engine | podman 5.7.0, rootless, cgroups v2, on WSL2 |
| Network plugin | kindnet, the kind default. It enforced the NetworkPolicies. Calico was not needed |
| Tracing backend | Jaeger 2.23.0 all-in-one, in memory, pinned by digest |
| Static checks | kubeconform v0.8.0 (strict), kube-linter v0.8.3 |

The saved output of the real run is in `evidence/`:

| File | Holds |
|---|---|
| `versions.txt` | Date, tool versions, the node, the image ID and build time |
| `static-checks.txt` | Output of `make lint` |
| `job-heal-loop-fixed.log`, `job-heal-loop-reverted.log` | The two Job logs |
| `verify.txt` | Output of `make verify`: 28 checks, all PASS |
| `verify-negative.txt` | Output of `make verify-negative`: 3 FAIL, as intended |
| `get-all.txt` | `kubectl get all,networkpolicy -n heal-lab -o wide`, taken last |
| `full-run.txt` | The whole `make lint cluster-up image deploy evidence` run, unedited |

The image in that run was built on 20 November 2026 at 22:04 UTC from the source as it stood then.
The application was being changed at the time, so a later build may differ.

In `get-all.txt` three policies are seconds old. That is because verify removes and re-applies them.

## Run it

Needs rootless podman, a systemd user session, `make`, `curl` and `openssl`. No sudo. No Docker.

```bash
cd deploy
make tools        # kind, kubectl, kubeconform, kube-linter into ~/.local/bin, each checked by SHA-256
make lint         # static checks, no cluster needed
make cluster-up   # one kind node under rootless podman, capped at 4 CPUs and 4 GB
make image        # build the image and load it into the node
make deploy       # apply the manifests, wait for Jaeger, run both Jobs
make verify       # every proof below; PASS or FAIL per check; non-zero exit on any FAIL
make evidence     # save versions, state, logs and verify output into evidence/
make cluster-down # delete the cluster
```

`make verify-negative` is optional. It shows that verify fails when a policy is wrong.

From `lint` to `evidence` took under five minutes with the images cached. The first run also
downloads more than 1 GB of images. `kubectl` uses `deploy/.kubeconfig`, so
`~/.kube/config` is not touched. kubectl does keep a small discovery cache in `~/.kube/cache`. To look around by hand: `export KUBECONFIG=$PWD/.kubeconfig`.

## Files

| File | What it does |
|---|---|
| `Containerfile` | Two-stage build. Stage one has uv and installs from `uv.lock`. Stage two has Python, git and the installed package. Runs as UID 10001. Base images pinned by digest. |
| `kind-config.yaml` | One node. Node image pinned by digest. |
| `k8s/00-namespace.yaml` | Namespace `heal-lab`, with Pod Security Admission `restricted` in enforce mode. |
| `k8s/10-config.yaml` | A ServiceAccount with no token, and the ConfigMap that becomes the workload's environment. |
| `k8s/20-jaeger.yaml` | Jaeger Deployment and the Service `lab-jaeger` (4318 for traces, 16686 for the UI). |
| `k8s/30-job-fixed.yaml` | The workload. Runs `start` then `resume --approve`. Must log `FIXED`. |
| `k8s/31-job-reverted.yaml` | The same with `start --bad-fix`. Must log `REVERTED`. |
| `k8s/40-networkpolicy.yaml` | Default deny in and out. Then: workload to Jaeger on 4318, workload to DNS, Jaeger accepts the workload on 4318. |
| `k8s/50-test.yaml` | Test fixtures only: `lab-probe` (the workload's settings and label, but it sleeps) and `lab-blocked` (a web server the workload may not reach). |
| `negative/allow-all-egress.yaml` | A deliberately wrong policy. Used by `make verify-negative` to show that verify can fail. Not deployed. |
| `scripts/tools.sh` | Installs the four pinned tools. |
| `scripts/verify.sh` | The proofs. |
| `Makefile` | The targets above. |

The Secret is not in a file. `make deploy` makes a random value with `openssl rand`, pipes it into
`kubectl create secret`, and never prints it or writes it to disk. It is a dummy. The application
does not read it. It is there to show the pattern: a secret arrives as a read-only file, not as an
environment variable.

## Which check proves what

All of these are in `scripts/verify.sh`. The output of the real run is `evidence/verify.txt`.

| Claim | Check | How |
|---|---|---|
| The loop runs end to end in the cluster | `job-fixed-complete`, `job-fixed-outcome` | Job condition is `Complete` and the log has a line starting `FIXED:` |
| A bad fix is reverted | `job-reverted-complete`, `job-reverted-outcome` | Same, with `REVERTED:` |
| Traces reach Jaeger over the allowed path | `traces-reached-jaeger` | Jaeger lists the service `heal-loop`. The Jobs ran with the policies applied |
| The probe pod stands in fairly for the Jobs | `probe-matches-job-*` | Security context, resources, service account and image are identical, as stored by Kubernetes |
| Not root | `uid-is-not-root` | `id -u` prints 10001 |
| Read-only root file system | `root-fs-is-read-only`, `volume-is-writable` | `touch /probe-test` fails with "Read-only file system". `touch /work/probe-test` works |
| No service account token | `no-service-account-token` | `/var/run/secrets/kubernetes.io` does not exist |
| No capabilities, no escalation, seccomp on | `no-capabilities`, `no-new-privileges`, `seccomp-filter-on` | `CapEff` is zero, `NoNewPrivs` is 1, `Seccomp` is 2 (filter) in `/proc/self/status` |
| The memory limit is real | `memory-limit-applied` | The pod's cgroup `memory.max` is 268435456 (256Mi) |
| No build tools in the image | `no-build-tools-in-image` | `uv`, `pip`, `gcc`, `cc` and `make` are not on the path |
| Secret is a file, not an environment variable | `secret-mounted-as-file`, `secret-not-in-environment` | The file's hash equals the Secret's hash. The value is not in `env` |
| The namespace refuses unsafe pods | `psa-refuses-privileged-pod` | A server-side dry run of a privileged pod is refused with `violates PodSecurity "restricted"` |
| The probes can succeed at all (positive control) | `before-*-open` | With the policies removed, the probe reaches the test server, the Jaeger UI, the Kubernetes API and 1.1.1.1:443 |
| Default deny works (negative control) | `after-*-blocked` | With the policies applied, the same four probes time out. The names still resolve |
| The one allowed path works | `after-jaeger-otlp-open` | The probe still reaches `lab-jaeger:4318` |
| verify can fail | `make verify-negative` | With an allow-all egress policy added, verify reports FAIL and exits non-zero (`evidence/verify-negative.txt`) |

Two notes on the network test.

`lab-blocked` accepts connections from any pod in the namespace. That is on purpose. It leaves the
workload's egress policy as the only thing in the way, so a blocked probe can only mean that policy.

In the negative run the workload's egress is wide open, and the Jaeger UI port still times out.
That is the Jaeger ingress policy on its own. It is the only direct evidence here for an ingress
rule.

## Static checks

`make lint` runs both. Output of the real run: `evidence/static-checks.txt`.

- **kubeconform**, strict mode, Kubernetes 1.37.0 schemas: 14 resources, 14 valid.
- **kube-linter**, default checks: one finding, fixed. Both Jobs had no `ttlSecondsAfterFinished`.
  It is now 3600, so a finished Job is removed after an hour. Result: no lint errors.

kube-linter's optional checks were run once with `--add-all-built-in` and not adopted. They ask for
three replicas, node affinity, a rolling update strategy, owner and email labels, DNS options,
sorted keys, and liveness probes on pods that run once. None of that fits a one-node demonstration.

Choices a reviewer may question:

- `imagePullPolicy: Never` and a tag, not a digest, on the application image. There is no registry,
  so there is no registry digest. The image ID is recorded in `evidence/versions.txt`.
- The Jaeger pod uses the same token-less ServiceAccount as the workload. Neither uses the API.

## Limits

- **This is a demonstration, not production Kubernetes experience.** It is one kind node under
  rootless podman on one machine. No ingress, no registry, no admission controller beyond Pod
  Security Admission, no multi-node scheduling, no upgrades, no storage classes in use, no RBAC
  rules, no autoscaling.
- **The human approval gate is not exercised.** The Job runs `resume --approve` by itself.
- **The hardening checks run in `lab-probe`, not in the Job pods.** A finished pod cannot be
  entered. verify shows the settings are identical, and then tests the probe.
- **The pod is the boundary.** The loop and the untrusted job it reruns share one container, one
  volume and the mounted secret file. The original platform confines each agent run separately.
- **The limits are set, not stress-tested.** verify reads `memory.max`. Nothing here tries to
  exceed a limit.
- **The network test covers TCP to four destinations.** It does not test UDP, or every port.
  The internet probe needs the host to be online. If it is not, the control fails and says so.
- **The Secret is a dummy and is only base64 in etcd.** No encryption at rest, no external store.
- **Jaeger has no authentication and keeps traces in memory.** Pods cannot reach its UI. A cluster
  admin can, through the API server.
- **The image is not scanned or signed.** No vulnerability scan, no SBOM, no signature. `git` comes
  from Debian's package index at build time, so its version is not pinned. The base is Debian slim,
  not distroless: a shell, `apt` and `dpkg` are still in the image.
- **Jaeger logged one export timeout for its own start-up traces.** The cause was not looked into.
  Traces from the workload arrived.
- **kube-linter's binary is pinned by a hash recorded on first download.** The project publishes
  sigstore bundles, and they were not verified. kind, kubectl and kubeconform are checked against
  the checksums their projects publish.
- **kubeconform downloads its schemas when it runs.**
- **Rootless has a side effect.** The whole cluster runs inside a user namespace, so root in the
  node is an ordinary user on the host. That comes from podman, not from these manifests.
- **Host cost.** The cluster used about 50 of this machine's 128 per-user inotify instances.
  Other programs of the same user share that limit.
- **What stays behind after `make cluster-down`.** The pulled and built images stay in podman's
  store, and `.kubeconfig` stays as an empty file with no key in it.
