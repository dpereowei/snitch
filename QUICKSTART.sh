#!/bin/bash
# Quick reference for Snitch deployment

echo "Snitch Deployment Quick Start"
echo "=================================="
echo ""

# Check dependencies
echo "1. Prerequisites:"
if command -v docker &> /dev/null; then
    echo "   ✓ Docker found"
else
    echo "   ✗ Docker not found - install from https://docs.docker.com/get-docker/"
    exit 1
fi

if command -v kubectl &> /dev/null; then
    echo "   ✓ kubectl found"
else
    echo "   ✗ kubectl not found - install from https://kubernetes.io/docs/tasks/tools/"
    exit 1
fi

# Check cluster connection
echo ""
echo "2. Kubernetes Cluster:"
if kubectl cluster-info &> /dev/null; then
    echo "   ✓ Connected to cluster"
    kubectl config current-context
else
    echo "   ✗ Not connected to a cluster"
    echo "   Run: kubectl config use-context <cluster-name>"
    exit 1
fi

# Build
echo ""
echo "3. Build Docker Image:"
echo "   docker build -t snitch:latest ."
echo ""

# Deploy
echo "4. Deploy to Kubernetes:"
echo ""
echo "   Step A: Update Slack webhook in k8s-deployment.yaml"
echo "   - Line 84: Replace 'YOUR/WEBHOOK/URL' with actual webhook URL"
echo ""
echo "   Step B: Deploy manifests"
echo "   kubectl apply -f k8s-deployment.yaml"
echo ""
echo "   Step C: Verify deployment"
echo "   kubectl get pods -n monitoring"
echo "   kubectl logs -f -n monitoring deployment/snitch"
echo ""

# Monitoring
echo "5. Monitoring:"
echo "   kubectl exec -it -n monitoring deployment/snitch -- sh -c \"sqlite3 /var/lib/snitch/snitch_events.db '.tables'\""
echo ""

# Cleanup
echo "6. Cleanup (if needed):"
echo "   kubectl delete -f k8s-deployment.yaml"
echo ""
