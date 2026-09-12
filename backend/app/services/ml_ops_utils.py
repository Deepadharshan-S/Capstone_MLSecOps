import os
import json
from app.core.config import settings


def to_k8s_endpoint(url: str) -> str:
    """
    Translates localhost / 127.0.0.1 URLs to host.docker.internal
    so that Kubernetes pods can communicate with host Docker services.
    """
    return url.replace("localhost", "host.docker.internal").replace(
        "127.0.0.1", "host.docker.internal"
    )


def get_scoped_training_credentials(job_id: str) -> dict:
    """
    Generates least-privilege credentials for Ray training and serving jobs.
    Attempts to generate temporary, 1-hour MinIO STS session credentials
    scoped strictly to the MLflow artifact storage bucket (s3://mlflow/*).
    Falls back to dedicated training credentials (MINIO_TRAINING_ACCESS_KEY_ID /
    LAKEFS_TRAINING_ACCESS_KEY_ID) or default configured credentials.
    """
    creds = {
        "aws_access_key_id": settings.MINIO_TRAINING_ACCESS_KEY_ID
        or os.getenv("AWS_ACCESS_KEY_ID", settings.MINIO_ROOT_USER),
        "aws_secret_access_key": settings.MINIO_TRAINING_SECRET_ACCESS_KEY
        or os.getenv("AWS_SECRET_ACCESS_KEY", settings.MINIO_ROOT_PASSWORD),
        "aws_session_token": "",
        "lakefs_access_key_id": settings.LAKEFS_TRAINING_ACCESS_KEY_ID
        or settings.LAKEFS_ACCESS_KEY_ID,
        "lakefs_secret_access_key": settings.LAKEFS_TRAINING_SECRET_ACCESS_KEY
        or settings.LAKEFS_SECRET_ACCESS_KEY,
        "scoped_sts": False,
    }

    if not settings.MINIO_TRAINING_ACCESS_KEY_ID:
        try:
            import boto3
            from botocore.client import Config

            sts_client = boto3.client(
                "sts",
                endpoint_url=settings.MINIO_ENDPOINT,
                aws_access_key_id=settings.MINIO_ROOT_USER,
                aws_secret_access_key=settings.MINIO_ROOT_PASSWORD,
                config=Config(
                    signature_version="s3v4",
                    connect_timeout=1,
                    read_timeout=1,
                    retries={"max_attempts": 1},
                ),
                region_name="us-east-1",
            )
            policy_doc = json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": [
                                "s3:GetBucketLocation",
                                "s3:ListBucket",
                                "s3:GetObject",
                                "s3:PutObject",
                            ],
                            "Resource": [
                                "arn:aws:s3:::mlflow",
                                "arn:aws:s3:::mlflow/*",
                            ],
                        }
                    ],
                }
            )
            response = sts_client.assume_role(
                RoleArn="arn:aws:iam:::role/RayTrainingJobRole",
                RoleSessionName=f"rayjob-{job_id[:16]}",
                Policy=policy_doc,
                DurationSeconds=3600,
            )
            sts_creds = response.get("Credentials", {})
            if sts_creds.get("AccessKeyId"):
                creds["aws_access_key_id"] = sts_creds["AccessKeyId"]
                creds["aws_secret_access_key"] = sts_creds["SecretAccessKey"]
                creds["aws_session_token"] = sts_creds.get("SessionToken", "")
                creds["scoped_sts"] = True
        except Exception:
            pass

    return creds
