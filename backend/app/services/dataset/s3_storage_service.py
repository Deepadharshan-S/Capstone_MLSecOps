from typing import Optional
import boto3
from botocore.client import Config
from app.core.config import settings
from app.services.interfaces import ObjectStorageService

class S3StorageService(ObjectStorageService):
    """
    Concrete implementation of ObjectStorageService that wraps boto3 resource calls
    to interact with MinIO / S3 object storage.
    """
    def delete_objects_with_prefix(self, bucket_name: str, prefix: str) -> None:
        """Deletes all objects in a bucket under the given prefix."""
        s3 = boto3.resource(
            "s3",
            endpoint_url=settings.MINIO_ENDPOINT,
            aws_access_key_id=settings.MINIO_ROOT_USER,
            aws_secret_access_key=settings.MINIO_ROOT_PASSWORD,
            config=Config(signature_version="s3v4"),
            region_name="us-east-1",
        )
        bucket = s3.Bucket(bucket_name)
        bucket.objects.filter(Prefix=prefix).delete()

    def check_health(self) -> tuple[bool, str]:
        """Checks connection/health of the MinIO / S3 object storage backend."""
        try:
            s3 = boto3.client(
                "s3",
                endpoint_url=settings.MINIO_ENDPOINT,
                aws_access_key_id=settings.MINIO_ROOT_USER,
                aws_secret_access_key=settings.MINIO_ROOT_PASSWORD,
                config=Config(signature_version="s3v4", connect_timeout=3, retries={"max_attempts": 1}),
                region_name="us-east-1",
            )
            s3.list_buckets()
            return True, "connected"
        except Exception as e:
            return False, f"error: {str(e)}"

    def put_log_content(self, bucket_name: str, key: str, content: str) -> None:
        """Uploads log text content to MinIO / S3 object storage."""
        s3 = boto3.client(
            "s3",
            endpoint_url=settings.MINIO_ENDPOINT,
            aws_access_key_id=settings.MINIO_ROOT_USER,
            aws_secret_access_key=settings.MINIO_ROOT_PASSWORD,
            config=Config(signature_version="s3v4"),
            region_name="us-east-1",
        )
        s3.put_object(
            Bucket=bucket_name,
            Key=key,
            Body=content.encode("utf-8"),
            ContentType="text/plain",
        )

    def get_log_content(self, bucket_name: str, key: str) -> Optional[str]:
        """Retrieves log text content from MinIO / S3 object storage if exists."""
        try:
            s3 = boto3.client(
                "s3",
                endpoint_url=settings.MINIO_ENDPOINT,
                aws_access_key_id=settings.MINIO_ROOT_USER,
                aws_secret_access_key=settings.MINIO_ROOT_PASSWORD,
                config=Config(signature_version="s3v4"),
                region_name="us-east-1",
            )
            response = s3.get_object(Bucket=bucket_name, Key=key)
            return response["Body"].read().decode("utf-8")
        except Exception:
            return None

