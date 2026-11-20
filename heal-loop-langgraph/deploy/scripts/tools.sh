#!/usr/bin/env bash
# Install pinned CLI tools into ~/.local/bin. No sudo. Each download is checked against a SHA-256.
set -euo pipefail

KIND=v0.33.0
KUBECTL=v1.37.2
KUBECONFORM=v0.8.0
KUBE_LINTER=v0.8.3
# kube-linter publishes sigstore bundles but no checksum file, and cosign is not installed here.
# This hash was recorded from the first download on 2026-11-20. It pins the binary; it does not prove who built it.
KUBE_LINTER_SHA256=618d299a3e2839c8ca9d86fce0db617be0fba41f0fecbbbfb7fbf1c04299fae1

BIN="$HOME/.local/bin"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$BIN"
cd "$TMP"

fetch() { curl -fsSL --retry 3 -o "$1" "$2"; }
check() { echo "$1  $2" | sha256sum -c - >/dev/null && echo "sha256 ok: $2"; } # check <hash> <file>

# kind: the release publishes "<hash>  kind-linux-amd64" next to the binary.
fetch kind "https://github.com/kubernetes-sigs/kind/releases/download/$KIND/kind-linux-amd64"
fetch kind.sha256sum "https://github.com/kubernetes-sigs/kind/releases/download/$KIND/kind-linux-amd64.sha256sum"
check "$(cut -d' ' -f1 kind.sha256sum)" kind

# kubectl: dl.k8s.io publishes the bare hash.
fetch kubectl "https://dl.k8s.io/release/$KUBECTL/bin/linux/amd64/kubectl"
fetch kubectl.sha256 "https://dl.k8s.io/release/$KUBECTL/bin/linux/amd64/kubectl.sha256"
check "$(cat kubectl.sha256)" kubectl

# kubeconform: one CHECKSUMS file for all archives.
fetch kubeconform.tar.gz "https://github.com/yannh/kubeconform/releases/download/$KUBECONFORM/kubeconform-linux-amd64.tar.gz"
fetch CHECKSUMS "https://github.com/yannh/kubeconform/releases/download/$KUBECONFORM/CHECKSUMS"
check "$(grep 'kubeconform-linux-amd64.tar.gz$' CHECKSUMS | cut -d' ' -f1)" kubeconform.tar.gz
tar -xzf kubeconform.tar.gz kubeconform

fetch kube-linter "https://github.com/stackrox/kube-linter/releases/download/$KUBE_LINTER/kube-linter-linux"
check "$KUBE_LINTER_SHA256" kube-linter

install -m 0755 kind kubectl kubeconform kube-linter "$BIN/"
echo "installed into $BIN: kind $KIND, kubectl $KUBECTL, kubeconform $KUBECONFORM, kube-linter $KUBE_LINTER"
