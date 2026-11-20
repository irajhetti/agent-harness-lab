#!/usr/bin/env bash
# Proves the claims in README.md against the running cluster.
# Prints one PASS or FAIL line per check, with what was seen. Exits non-zero if any check failed.
# Run `make deploy` first. Safe to run again.
set -uo pipefail
cd "$(dirname "$0")/.."
export KUBECONFIG="${KUBECONFIG:-$PWD/.kubeconfig}"
K="kubectl -n heal-lab"
fails=0

# check <name> <regex> <command...>
# PASS when a line of the command's output matches the regex. A check never passes just because a command
# failed: even "this must be refused" checks look for the exact refusal text.
# TRIES=n retries every 2 seconds, for checks that wait for a NetworkPolicy change to take effect.
check() {
  local name=$1 want=$2 got line
  shift 2
  for _ in $(seq "${TRIES:-1}"); do
    got=$("$@" 2>&1)
    line=$(grep -E -m1 -- "$want" <<<"$got") && break
    sleep 2
  done
  if [ -n "$line" ]; then
    printf 'PASS  %-34s %s\n' "$name" "$line"
  else
    printf 'FAIL  %-34s wanted /%s/ but saw: %s\n' "$name" "$want" "$(tail -n 3 <<<"$got" | tr '\n' ' ')"
    fails=$((fails + 1))
  fi
}

in_probe() { $K exec lab-probe -- "$@"; }

# Open a TCP connection from inside lab-probe, which carries the workload's label.
# Prints OPEN, or BLOCKED with the error, or NO-DNS. BLOCKED means the name resolved and the connection failed.
reach() {
  in_probe python -c '
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
try:
    ip = socket.gethostbyname(host)
except OSError as error:
    sys.exit(f"NO-DNS {host}: {error}")
try:
    socket.create_connection((ip, port), timeout=3).close()
    print(f"OPEN {host}:{port} ({ip})")
except OSError as error:
    print(f"BLOCKED {host}:{port} ({ip}): {error}")
' "$1" "$2"
}

# The settings that matter, as Kubernetes stores them, for comparing the Jobs with the probe pod.
settings() { # settings <object> <path to the pod spec>
  $K get "$1" -o jsonpath="{$2.securityContext}{$2.containers[0].securityContext}{$2.containers[0].resources}{$2.automountServiceAccountToken}{$2.serviceAccountName}{$2.containers[0].image}"
}
same_as_probe() {
  if [ "$(settings "job/$1" .spec.template.spec)" = "$(settings pod/lab-probe .spec)" ]; then echo "same settings as lab-probe"; else echo different; fi
}

echo "== 1. The workload ran"
check job-fixed-complete '(^| )Complete' $K get job heal-loop-fixed -o 'jsonpath={.status.conditions[*].type}'
check job-fixed-outcome '^FIXED: ' $K logs job/heal-loop-fixed
check job-reverted-complete '(^| )Complete' $K get job heal-loop-reverted -o 'jsonpath={.status.conditions[*].type}'
check job-reverted-outcome '^REVERTED: ' $K logs job/heal-loop-reverted
# Asked through the API server, because pods may not reach the Jaeger UI port. Fails if the image has no tracing.
check traces-reached-jaeger '"heal-loop"' kubectl get --raw /api/v1/namespaces/heal-lab/services/lab-jaeger:16686/proxy/api/v3/services

echo "== 2. Runtime hardening, checked inside lab-probe (same settings as the Jobs)"
check probe-matches-job-fixed '^same' same_as_probe heal-loop-fixed
check probe-matches-job-reverted '^same' same_as_probe heal-loop-reverted
check uid-is-not-root '^10001$' in_probe id -u
check root-fs-is-read-only 'Read-only file system' in_probe touch /probe-test
check volume-is-writable '^wrote$' in_probe sh -c 'touch /work/probe-test && echo wrote'
check no-service-account-token '^absent$' in_probe sh -c 'test ! -e /var/run/secrets/kubernetes.io && echo absent'
check no-capabilities '^CapEff:\s+0000000000000000$' in_probe grep CapEff /proc/self/status
check no-new-privileges '^NoNewPrivs:\s+1$' in_probe grep NoNewPrivs /proc/self/status
check seccomp-filter-on '^Seccomp:\s+2$' in_probe grep Seccomp: /proc/self/status
check memory-limit-applied '^268435456$' in_probe cat /sys/fs/cgroup/memory.max
check no-build-tools-in-image '^none$' in_probe sh -c 'found=$(for tool in uv pip gcc cc make; do command -v $tool; done); echo "${found:-none}"'
# Compares hashes, so the value is never printed.
secret_hash() { $K get secret heal-loop-demo -o 'jsonpath={.data.demo-token}' | base64 -d | sha256sum; }
check secret-mounted-as-file "^$(secret_hash)\$" in_probe sh -c 'sha256sum < /run/secrets/heal-loop/demo-token'
check secret-not-in-environment '^not in env$' in_probe sh -c 'env | grep -qF "$(cat /run/secrets/heal-loop/demo-token)" || echo "not in env"'

echo "== 3. Pod Security Admission refuses a privileged pod (server-side dry run, nothing is created)"
check psa-refuses-privileged-pod 'violates PodSecurity "restricted' \
  $K run psa-test --image=localhost/heal-loop:0.1.0 --privileged --dry-run=server

echo "== 4. Network policy: controls first, with the policies removed"
$K delete -f k8s/40-networkpolicy.yaml --ignore-not-found >/dev/null
TRIES=15 check before-blocked-server-open '^OPEN' reach lab-blocked 8080
TRIES=15 check before-jaeger-ui-open '^OPEN' reach lab-jaeger 16686
TRIES=15 check before-kube-api-open '^OPEN' reach kubernetes.default.svc 443
TRIES=15 check before-internet-open '^OPEN' reach 1.1.1.1 443

echo "== 5. Network policy: the same probes with the policies applied"
$K apply -f k8s/40-networkpolicy.yaml >/dev/null
TRIES=15 check after-blocked-server-blocked '^BLOCKED .*timed out' reach lab-blocked 8080
TRIES=15 check after-jaeger-ui-blocked '^BLOCKED .*timed out' reach lab-jaeger 16686
TRIES=15 check after-kube-api-blocked '^BLOCKED .*timed out' reach kubernetes.default.svc 443
TRIES=15 check after-internet-blocked '^BLOCKED .*timed out' reach 1.1.1.1 443
check after-jaeger-otlp-open '^OPEN' reach lab-jaeger 4318 # OPEN also shows DNS still works

echo
if [ "$fails" -eq 0 ]; then echo "RESULT: all checks passed"; else echo "RESULT: $fails check(s) FAILED"; fi
[ "$fails" -eq 0 ]
