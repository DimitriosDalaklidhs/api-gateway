#!/usr/bin/env bash
# Roll the mock service and the gateway to the images built from one commit,
# and roll back whichever of them does not become ready.
#
#   deploy.sh <7-character commit SHA>
#
# The SHA may also arrive in SSH_ORIGINAL_COMMAND. That is how the CD workflow
# calls this script: through an SSH key that is allowed to run nothing else
# (README, "Automatic deploys").
set -euo pipefail

sha="${1:-${SSH_ORIGINAL_COMMAND:-}}"

# Checked before it goes anywhere near a command line: over SSH this value
# comes from outside
if [[ ! "$sha" =~ ^[0-9a-f]{7}$ ]]; then
  echo "usage: deploy.sh <7-character commit SHA>" >&2
  exit 2
fi

image="ghcr.io/dimitriosdalaklidhs/api-gateway"
namespace="gateway"

# roll <deployment> <container> <tag>
roll() {
  kubectl -n "$namespace" set image "deployment/$1" "$2=$image:$3"
  # Waits until the new pods pass their readiness probe. A tag that doesn't
  # exist or an image that won't start ends here, with the old pods still up.
  if ! kubectl -n "$namespace" rollout status "deployment/$1" --timeout=180s; then
    echo "$1 did not become ready on $3, rolling back" >&2
    kubectl -n "$namespace" rollout undo "deployment/$1"
    exit 1
  fi
}

# The downstream first, then the gateway in front of it
roll mock-service mock-service "mock-$sha"
roll gateway gateway "gateway-$sha"

echo "deployed $sha"
