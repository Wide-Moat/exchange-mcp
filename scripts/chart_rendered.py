"""Checks on a rendered chart that grep cannot make reliably.

    python scripts/chart_rendered.py rendered.yaml

The YAML is read through `yq` (on every GitHub-hosted runner), so the check needs no Python package.

- no container (init or server) has a CPU limit: it throttles, and a VPA setting requests fights it;
- every probe uses the metrics port, which the Ingress never routes;
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
    for d in deployments:
        pod = d["spec"]["template"]["spec"]
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
