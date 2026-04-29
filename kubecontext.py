#!/usr/bin/env python3
"""
Kubernetes context management - handles both in-cluster and out-of-cluster authentication.
"""

import logging
import os
from pathlib import Path
from typing import Optional

from kubernetes import client, config
from kubernetes.client.api_client import ApiClient

logger = logging.getLogger("snitch.kubecontext")


def load_kubeconfig() -> ApiClient:
    """
    Load Kubernetes configuration with automatic context detection.
    
    Strategy:
    1. Try to load in-cluster configuration (ServiceAccount)
    2. Fall back to kubeconfig file (~/.kube/config)
    3. Raise error if neither works
    
    Returns:
        ApiClient: Configured Kubernetes API client
        
    Raises:
        Exception: If no valid Kubernetes configuration is found
    """
    
    # Try in-cluster configuration (ServiceAccount)
    if _is_in_cluster():
        try:
            logger.info("Loading in-cluster Kubernetes configuration")
            config.load_incluster_config()
            return client.ApiClient()
        except config.config_exception.ConfigException as e:
            logger.warning(f"Failed to load in-cluster config: {e}")
    
    # Fall back to kubeconfig file
    try:
        logger.info("Loading kubeconfig from filesystem")
        config.load_kube_config()
        return client.ApiClient()
    except config.config_exception.ConfigException as e:
        logger.error(f"Failed to load kubeconfig: {e}")
        raise RuntimeError(
            "Could not load Kubernetes configuration. "
            "Not running in a cluster and kubeconfig not found at ~/.kube/config"
        ) from e


def _is_in_cluster() -> bool:
    """
    Detect if code is running inside a Kubernetes cluster.
    
    Checks for:
    - KUBERNETES_SERVICE_HOST environment variable
    - KUBERNETES_SERVICE_PORT environment variable  
    - /var/run/secrets/kubernetes.io/serviceaccount/token (ServiceAccount token)
    
    Returns:
        bool: True if running in-cluster, False otherwise
    """
    service_host = os.getenv("KUBERNETES_SERVICE_HOST")
    service_port = os.getenv("KUBERNETES_SERVICE_PORT")
    token_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
    
    has_env_vars = service_host and service_port
    has_token = token_path.exists()
    
    is_in_cluster = has_env_vars and has_token
    
    if is_in_cluster:
        logger.debug(
            f"In-cluster indicators detected: "
            f"KUBERNETES_SERVICE_HOST={service_host}, "
            f"token_file={has_token}"
        )
    else:
        logger.debug("Not running in Kubernetes cluster")
    
    return is_in_cluster


def get_v1_client(api_client: Optional[ApiClient] = None) -> client.CoreV1Api:
    """
    Get Kubernetes CoreV1Api client.
    
    Args:
        api_client: Optional pre-configured ApiClient. If None, loads configuration.
        
    Returns:
        CoreV1Api: Configured Kubernetes Core API client
    """
    if api_client is None:
        api_client = load_kubeconfig()
    return client.CoreV1Api(api_client)


def get_batch_client(api_client: Optional[ApiClient] = None) -> client.BatchV1Api:
    """
    Get Kubernetes BatchV1Api client (for Jobs, CronJobs).
    
    Args:
        api_client: Optional pre-configured ApiClient. If None, loads configuration.
        
    Returns:
        BatchV1Api: Configured Kubernetes Batch API client
    """
    if api_client is None:
        api_client = load_kubeconfig()
    return client.BatchV1Api(api_client)


def get_apps_client(api_client: Optional[ApiClient] = None) -> client.AppsV1Api:
    """
    Get Kubernetes AppsV1Api client (for Deployments, StatefulSets, etc).
    
    Args:
        api_client: Optional pre-configured ApiClient. If None, loads configuration.
        
    Returns:
        AppsV1Api: Configured Kubernetes Apps API client
    """
    if api_client is None:
        api_client = load_kubeconfig()
    return client.AppsV1Api(api_client)
