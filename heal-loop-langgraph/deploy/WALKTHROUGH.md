# Walkthrough (to say out loud)

About 500 words. The checks named in `README.md` back each statement about the cluster. The
description of the original platform comes from the project README, not from a check here.

---

This folder packages my heal loop as a container and runs it on a small Kubernetes cluster on my
own machine. The cluster is kind, one node, under rootless podman. I built it to show how I would
confine an agent workload on Kubernetes, and to compare that with my original platform.

The image is a two-stage build. The first stage has uv and installs exactly what the lock file
says. The second has Python, git and the installed package, with no uv, pip or compiler. The base
images are pinned by digest. The container runs as user 10001.

The workload is a Job. It runs the scripted demo: `start`, which stops at the approval gate, then
`resume --approve`. It passes when the log ends with FIXED. A second Job scripts a bad fix and must
end with REVERTED. One honest point: in the Job the approval is automatic, because nobody is at a
terminal. So the cluster run shows the packaging and the confinement. It does not show the human
gate.

Every pod has the same security settings. A fixed non-root user. No privilege escalation. All
capabilities dropped. The default seccomp filter. A read-only root file system, with one writable
`emptyDir` volume at `/work`. No service account token, so the pod holds no credential for the
Kubernetes API. The namespace enforces the `restricted` Pod Security level, so the API server
refuses a pod that asks for more.

The network is default deny, in and out. The workload gets two allows: Jaeger on port 4318 for
traces, and cluster DNS. I tested this with controls. With the policies removed, a probe pod with
the workload's label reaches a test server, the Jaeger UI, the Kubernetes API and the internet.
With the policies applied, all four time out and the trace port still works. I also apply a wrong
policy once, to show that the test fails when it should.

Now the comparison. My original platform runs each agent under bubblewrap on one Linux host. The
file system is read-only except the worktree. Secret directories are masked. The environment is
scrubbed. The agent has a tool allow-list and deny rules.

Kubernetes covers much of that. The read-only root with one writable volume matches the read-only
file system with one writable worktree. A container sees only its image and the volumes I declare,
which does the job of masking secret directories. It does not inherit my shell's environment.
Dropped capabilities, no escalation and seccomp limit what a process can ask the kernel for. The
network policy and the missing token limit where it can connect and what it can call.

It does not cover everything. Kubernetes knows nothing about an agent's tools, so the allow-list
and deny rules stay in the application. Bubblewrap wraps each agent run separately from the code
that drives it. Here the pod is the boundary, so the loop and the untrusted job it reruns share one
container. To separate them I would run the rerun in its own pod. And a container shares the host
kernel, as bubblewrap does. It is not a virtual machine.

Last, the limits. This is one node on my own machine. I can explain it line by line. It is not
production Kubernetes experience, and I would not present it as that.
