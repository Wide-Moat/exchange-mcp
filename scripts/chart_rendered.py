"""Checks on a rendered chart that grep cannot make reliably.

    python scripts/chart_rendered.py rendered.yaml

The YAML is read through `yq` (on every GitHub-hosted runner), so the check needs no Python package.

- no container (init or server) has a CPU limit: it throttles, and a VPA setting requests fights it;
- the server container has an HTTP readiness probe on /readyz and liveness probe on /healthz, both on
  the metrics port, which the Ingress never routes; no probe anywhere uses another port;
- the metrics port is published only by a ClusterIP Service;
- there is at least one Deployment, so a render that produced nothing cannot pass.
"""

from __future__ import annotations

import json
import subprocess
import sys


def main(path: str) -> int:
    command = ["yq", "-o=json", "-I=0", ".", path]  # a fixed tool on a file this job rendered
    out = subprocess.run(command, capture_output=True, text=True, check=True).stdout  # noqa: S603
    docs = [d for d in (json.loads(line) for line in out.splitlines() if line.strip()) if d]
    deployments = [d for d in docs if d["kind"] == "Deployment"]
    problems = []
    if not deployments:
        problems.append("no Deployment rendered")
    for svc in (d for d in docs if d["kind"] == "Service"):
        ports = {p.get("name") for p in svc["spec"].get("ports", [])}
        if "metrics" in ports and svc["spec"].get("type", "ClusterIP") != "ClusterIP":
            problems.append(f"Service {svc['metadata']['name']} publishes metrics as {svc['spec']['type']}")
    for d in deployments:
        pod = d["spec"]["template"]["spec"]
        server = [c for c in pod["containers"] if c["name"] == "server"]
        if not server:
            problems.append("no server container")
        for c in server:
            for probe, path in (("readinessProbe", "/readyz"), ("livenessProbe", "/healthz")):
                got = (c.get(probe) or {}).get("httpGet") or {}
                if got.get("path") != path or got.get("port") != "metrics":
                    problems.append(f"server {probe} is not GET {path} on the metrics port: {got or 'absent'}")
        for c in pod["containers"] + pod.get("initContainers", []):
            if "cpu" in ((c.get("resources") or {}).get("limits") or {}):
                problems.append(f"{c['name']} has a CPU limit")
            for probe in ("readinessProbe", "livenessProbe", "startupProbe"):
                port = (c.get(probe) or {}).get("httpGet", {}).get("port")
                if port is not None and port != "metrics":
                    problems.append(f"{c['name']} {probe} uses port {port}, not metrics")
    for p in problems:
        print(p)
    print(f"{len(deployments)} Deployment(s) checked, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
