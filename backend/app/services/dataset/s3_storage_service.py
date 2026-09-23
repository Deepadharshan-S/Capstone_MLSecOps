import logging
from typing import Optional, Any
import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from app.core.config import settings
from app.services.interfaces import ObjectStorageService

logger = logging.getLogger("s3_storage_service")


class S3StorageService(ObjectStorageService):
    """
    Concrete implementation of ObjectStorageService that reuses a persistent
    thread-safe Boto3 client/resource to interact with MinIO / S3 object storage.
    Enforces least-privilege credentials via settings.minio_app_user and
    settings.minio_app_password, and supports dependency injection for testing.
    """

    def __init__(
        self,
        s3_client: Optional[Any] = None,
        s3_resource: Optional[Any] = None,
    ) -> None:
        self._custom_client = s3_client
        self._custom_resource = s3_resource

        if s3_client is not None and s3_resource is not None:
            self._client = s3_client
            self._resource = s3_resource
        else:
            session = boto3.Session(
                aws_access_key_id=settings.minio_app_user,
                aws_secret_access_key=settings.minio_app_password,
                region_name="us-east-1",
            )
            config = Config(
                signature_version="s3v4",
                connect_timeout=3,
                retries={"max_attempts": 2},
            )
            self._client = s3_client or session.client(
                "s3",
                endpoint_url=settings.MINIO_ENDPOINT,
                config=config,
            )
            self._resource = s3_resource or session.resource(
                "s3",
                endpoint_url=settings.MINIO_ENDPOINT,
                config=Config(signature_version="s3v4"),
            )

    @property
    def client(self):
        return self._client

    @property
    def resource(self):
        return self._resource

    def delete_objects_with_prefix(self, bucket_name: str, prefix: str) -> None:
        """Deletes all objects in a bucket under the given prefix using the reused S3 resource."""
        bucket = self._resource.Bucket(bucket_name)
        bucket.objects.filter(Prefix=prefix).delete()

    def check_health(self) -> tuple[bool, str]:
        """Checks connection/health of the MinIO / S3 object storage backend using the reused S3 client."""
        try:
            self._client.list_buckets()
            return True, "connected"
        except Exception as e:
            logger.warning(f"MinIO health check failed: {e}")
            return False, f"error: {str(e)}"

    def put_log_content(self, bucket_name: str, key: str, content: str) -> None:
        """Uploads log text content to MinIO / S3 object storage using the reused S3 client."""
        self._client.put_object(
            Bucket=bucket_name,
            Key=key,
            Body=content.encode("utf-8"),
            ContentType="text/plain",
        )

    def get_log_content(self, bucket_name: str, key: str) -> Optional[str]:
        """Retrieves log text content from MinIO / S3 object storage if exists."""
        try:
            response = self._client.get_object(Bucket=bucket_name, Key=key)
            return response["Body"].read().decode("utf-8")
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code in ["NoSuchKey", "404"]:
                logger.debug(f"Log content not found at s3://{bucket_name}/{key}")
                return None
            logger.warning(f"Error reading log content from s3://{bucket_name}/{key}: {e}")
            return None
        except Exception as e:
            logger.warning(f"Unexpected error retrieving s3://{bucket_name}/{key}: {e}")
            return None
