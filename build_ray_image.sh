#!/bin/bash
set -e

# Change directory to script location to ensure correct paths
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

echo "Building custom Ray Docker image (mlsecops-ray:latest)..."
docker build -t mlsecops-ray:latest -f Dockerfile.ray .

echo "Checking for local Kubernetes clusters to load the image..."

# Detect kind
if command -v kind &> /dev/null; then
    CLUSTERS=$(kind get clusters 2>/dev/null || true)
    if [ -n "$CLUSTERS" ]; then
        for cluster in $CLUSTERS; do
            echo "Loading image into kind cluster: $cluster..."
            kind load docker-image mlsecops-ray:latest --name "$cluster"
        done
    fi
fi

# Detect minikube
if command -v minikube &> /dev/null; then
    if minikube status &> /dev/null; then
        echo "Loading image into minikube cluster..."
        minikube image load mlsecops-ray:latest
    fi
fi

echo "Successfully built and prepared custom Ray image: mlsecops-ray:latest"
