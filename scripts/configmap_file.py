#!/usr/bin/env python3
"""
Print one file out of a ConfigMap manifest.

    configmap_file.py <manifest.yaml> <ConfigMap name> <key>

The gateway's config.yaml and Prometheus's prometheus.yml and alerts.yml are
not files in this repository; they are entries inside ConfigMaps. CI uses this
to get them out, so it can check Prometheus's config with promtool and start
the gateway image with the configuration the cluster gives it.
"""

import sys

import yaml


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    manifest, name, key = sys.argv[1:]

    with open(manifest) as f:
        # A manifest file can hold several documents separated by "---"
        for doc in yaml.safe_load_all(f):
            if doc and doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == name:
                if key not in doc.get("data", {}):
                    print(f"ConfigMap {name} has no key {key}", file=sys.stderr)
                    return 1
                sys.stdout.write(doc["data"][key])
                return 0

    print(f"no ConfigMap named {name} in {manifest}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
