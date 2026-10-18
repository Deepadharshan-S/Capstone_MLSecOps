#!/bin/bash
set -e

# Change directory to script location to ensure correct paths
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

ENV_FILE=".env"
[ ! -f "$ENV_FILE" ] && [ -f "backend/.env" ] && ENV_FILE="backend/.env"

DOTENV_RAY_IMAGE=""
if [ -f "$ENV_FILE" ]; then
    DOTENV_RAY_IMAGE="$(grep -E '^RAY_IMAGE=' "$ENV_FILE" | head -n 1 | cut -d= -f2- | tr -d '"'\''')"
fi

IMAGE_TAG="${RAY_IMAGE:-${DOTENV_RAY_IMAGE:-sentinelml-ray:latest}}"
echo "Building custom Ray Docker image ($IMAGE_TAG)..."
docker build -t "$IMAGE_TAG" -t sentinelml-ray:latest -t mlsecops-ray:latest -f Dockerfile.ray .

echo "Checking for local Kubernetes clusters to load the image..."

# Detect kind
if command -v kind &> /dev/null; then
    CLUSTERS=$(kind get clusters 2>/dev/null || true)
    if [ -n "$CLUSTERS" ]; then
        for cluster in $CLUSTERS; do
            echo "Loading image into kind cluster: $cluster..."
            kind load docker-image "$IMAGE_TAG" --name "$cluster"
            [ "$IMAGE_TAG" != "sentinelml-ray:latest" ] && kind load docker-image sentinelml-ray:latest --name "$cluster" 2>/dev/null || true
            kind load docker-image mlsecops-ray:latest --name "$cluster" 2>/dev/null || true
        done
    fi
fi

# Detect minikube
if command -v minikube &> /dev/null; then
    if minikube status &> /dev/null; then
        echo "Loading image into minikube cluster..."
        minikube image load "$IMAGE_TAG"
        [ "$IMAGE_TAG" != "sentinelml-ray:latest" ] && minikube image load sentinelml-ray:latest 2>/dev/null || true
        minikube image load mlsecops-ray:latest 2>/dev/null || true
    fi
fi

echo "Successfully built and prepared custom Ray image: $IMAGE_TAG"
