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
