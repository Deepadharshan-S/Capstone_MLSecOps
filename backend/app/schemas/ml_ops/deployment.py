from typing import Optional
from pydantic import BaseModel


class DeployModelSchema(BaseModel):
    model_id: str
    environment: str = "staging"
    version: Optional[str] = "latest"
    replicas: Optional[int] = 1


class ManageDeploymentSchema(BaseModel):
    deployment_id: str
    action: str = "restart"  # restart, rollback, stop


class DeployModelResponse(BaseModel):
    message: str
    model_id: str
    version: Optional[str] = "1"
    environment: str
    deployed_by: str
    status: str
    endpoint_url: Optional[str] = None
    rayservice_name: Optional[str] = None
    deployment_id: Optional[str] = None


class ManageDeploymentResponse(BaseModel):
    message: str
    deployment_id: str
    action_taken: str
    triggered_by: str


class DeploymentItem(BaseModel):
    deployment_id: str
    model_name: str
    version: str
    environment: str
    status: str
    rayservice_name: Optional[str] = None
    endpoint_url: Optional[str] = None
    deployed_by: Optional[str] = None
    deployed_at: Optional[str] = None
    k8s_status: Optional[str] = "Unknown"


class DeploymentListResponse(BaseModel):
    deployments: list[DeploymentItem]


class ReplicaHealth(BaseModel):
    desired_replicas: Optional[int] = 1
    ready_replicas: Optional[int] = 0
    active_worker_pods: Optional[int] = 0
    total_worker_pods: Optional[int] = 0
    head_pod_status: Optional[str] = "Unknown"
    details: Optional[dict] = {}


class DeploymentDetailResponse(BaseModel):
    deployment_id: str
    model_name: str
    version: str
    environment: str
    status: str
    rayservice_name: Optional[str] = None
    endpoint_url: Optional[str] = None
    internal_endpoints: Optional[dict[str, str]] = {}
    deployed_by: Optional[str] = None
    deployed_at: Optional[str] = None
    k8s_status: Optional[str] = "Unknown"
    service_status: Optional[str] = "Unknown"
    replica_health: ReplicaHealth = ReplicaHealth()
    ray_cluster_status: Optional[dict] = {}
    tags: Optional[dict[str, str]] = {}

