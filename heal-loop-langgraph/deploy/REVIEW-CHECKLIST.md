# Review checklist

Twelve checks. Run them from `deploy/`. Checks 6 to 11 need the cluster: `make cluster-up image deploy`,
then `export KUBECONFIG=$PWD/.kubeconfig`.

1. **The tools are pinned and checksummed.**
   `grep -n 'check \|=v' scripts/tools.sh`
   Four versions, four `check` calls. Note the comment on kube-linter: its hash was recorded on first
   download, not published by the project.

2. **Every image is pinned by digest.**
   `grep -n '@sha256' Containerfile kind-config.yaml k8s/*.yaml`
   Expect the Python base, the uv image, the kind node and Jaeger. The application image is local
   and has no registry digest.

3. **The final image has no build tools and does not run as root.**
   `podman run --rm --entrypoint sh localhost/heal-loop:0.1.0 -c 'id; for t in uv pip gcc make; do command -v $t; done; git --version'`
   Expect UID 10001, no tool paths, and a git version.

4. **The static checks pass.**
   `make lint`
   Expect 14 valid resources and "No lint errors found!".

5. **No secret is committed.**
   `grep -rn 'kind: Secret' . ; cat .gitignore`
   Expect no match. `.kubeconfig` holds the cluster admin key while the cluster exists and is ignored.

6. **Every pod carries the full security context.**
   `kubectl -n heal-lab get pods -o custom-columns='POD:.metadata.name,UID:.spec.securityContext.runAsUser,SECCOMP:.spec.securityContext.seccompProfile.type,RO:.spec.containers[0].securityContext.readOnlyRootFilesystem,ESC:.spec.containers[0].securityContext.allowPrivilegeEscalation,DROP:.spec.containers[0].securityContext.capabilities.drop,TOKEN:.spec.automountServiceAccountToken'`
   Expect `10001 RuntimeDefault true false [ALL] false` on every row.

7. **Nothing touches the host.**
   `grep -rnE 'hostPath|hostNetwork|hostPID|hostIPC|hostPort|privileged' k8s/`
   Expect no match.

8. **The Jobs did what the README says.**
   `kubectl -n heal-lab logs job/heal-loop-fixed | tail -1; kubectl -n heal-lab logs job/heal-loop-reverted | tail -1`
   Expect a `FIXED:` line and a `REVERTED:` line.

9. **The namespace refuses an unsafe pod.**
   `kubectl -n heal-lab run psa-test --image=localhost/heal-loop:0.1.0 --privileged --dry-run=server`
   Expect `Forbidden` and `violates PodSecurity "restricted"`. Nothing is created.

10. **The network policy is enforced, with controls.**
    `make verify`
    In section 4 the four `before-*` probes are OPEN. In section 5 the same four are BLOCKED with
    "timed out" and `after-jaeger-otlp-open` is OPEN. Read `k8s/40-networkpolicy.yaml` next to it.

11. **verify can fail.**
    `make verify-negative`
    Expect FAIL lines for the blocked server, the Kubernetes API and the internet, then
    "EXPECTED: verify failed". The Jaeger UI line should still PASS: its ingress rule holds on its own.

12. **The evidence is real and nothing is left running.**
    Compare `evidence/verify.txt` with your own `make verify`. Pod IPs and the secret hash will
    differ. Then `make cluster-down; podman ps; kind get clusters`.
    Expect no `heal-lab` container and no cluster. Then read the Limits section of `README.md`
    and ask whether any claim in `WALKTHROUGH.md` goes beyond it.
