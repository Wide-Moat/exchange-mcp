#!/usr/bin/env bash
# Build the image once for amd64 and arm64, then check exactly what was built: each architecture is
# scanned (no HIGH or CRITICAL vulnerability with a fix) and started (the server's modules import on it).
# publish.yml pushes this same archive afterwards, so what is pushed is what was checked; validate.yml
# runs the script on every pull request, so the path publish depends on is exercised before merge.
#
#   TRIVY=/path/to/trivy scripts/build-image.sh <out dir> [--label k=v ...]
#
# Leaves <out dir>/image.tar, an OCI archive holding the manifest list. Needs docker buildx with a
# container builder, QEMU for the foreign architecture, skopeo and trivy.
set -euo pipefail
cd "$(dirname "$0")/.."
out=${1:?usage: build-image.sh <out dir> [--label k=v ...]}
shift
trivy=${TRIVY:-trivy}
command -v skopeo >/dev/null || { echo "skopeo is required" >&2; exit 2; }
mkdir -p "$out"
layouts=$(mktemp -d)  # per-architecture layouts, this run's own
trap 'rm -rf "$layouts"' EXIT

# both architectures, the foreign one under QEMU: a stalled registry or mirror fails the build in time
timeout "${BUILD_SECONDS:-1500}" \
  docker buildx build --platform linux/amd64,linux/arm64 "$@" --output "type=oci,dest=$out/image.tar" .
digest=$(skopeo inspect --raw "oci-archive:$out/image.tar" | sha256sum | cut -d' ' -f1)
echo "built the manifest list sha256:${digest}"

status=0
for arch in amd64 arm64; do
  # one architecture, as an OCI layout directory: what trivy --input reads
  skopeo copy --quiet --override-arch "$arch" "oci-archive:$out/image.tar" "oci:$layouts/$arch:image"
  got=$(skopeo inspect --config "oci:$layouts/$arch:image" | jq -r .architecture)
  [ "$got" = "$arch" ] || { echo "the $arch image is $got" >&2; exit 1; }

  "$trivy" image --no-progress --scanners vuln --severity HIGH,CRITICAL --ignore-unfixed --list-all-pkgs \
    --format json -o "$out/scan-$arch.json" --input "$layouts/$arch"
  python3 - "$out/scan-$arch.json" "$arch" <<'PY' || status=1
import json, sys
d = json.load(open(sys.argv[1]))
results = d.get("Results") or []
packages = sum(len(r.get("Packages") or []) for r in results)
vulns = [v for r in results for v in (r.get("Vulnerabilities") or [])]
print(f"{sys.argv[2]}: {len(results)} targets, {packages} packages, {len(vulns)} fixable HIGH/CRITICAL")
if not packages:
    print("trivy found no packages: the image was not scanned"); sys.exit(2)
for v in vulns:
    print(v["VulnerabilityID"], v.get("PkgName"), v.get("InstalledVersion"), "->", v.get("FixedVersion"))
sys.exit(1 if vulns else 0)
PY

  skopeo copy --quiet "oci:$layouts/$arch:image" "docker-daemon:exchange-mcp:check-$arch"
  machine=$(docker run --rm --platform "linux/$arch" --entrypoint python "exchange-mcp:check-$arch" \
    -c 'import platform, exchange_mcp.app, redis, prometheus_client; print(platform.machine())')
  want=$([ "$arch" = amd64 ] && echo x86_64 || echo aarch64)
  [ "$machine" = "$want" ] || { echo "the $arch image runs as $machine" >&2; exit 1; }
  echo "$arch: imports and runs as $machine"
done
exit "$status"
