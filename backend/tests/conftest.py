import pytest
import lakefs
import boto3
from botocore.client import Config

from app.services.data_service import data_service

@pytest.fixture(scope="session", autouse=True)
def cleanup_after_tests():
    # Let all tests run first
    yield

    # Clean up test-specific lakeFS repositories and their matching MinIO storage prefixes
    try:
        repos = list(lakefs.repositories(client=data_service.client))
        s3 = boto3.resource(
            's3',
            endpoint_url='http://localhost:9000',
            aws_access_key_id='minioadmin',
            aws_secret_access_key='minioadmin123',
            config=Config(signature_version='s3v4'),
            region_name='us-east-1'
        )
        bucket = s3.Bucket('lakefs')

        test_repo_names = {"iris-dataset-admin", "iris-dataset-ds", "iris-dataset-2"}

        for repo in repos:
            repo_id = repo.id
            # Identify if the repository was created by the test suite
            if repo_id.startswith("test-dataset-") or repo_id in test_repo_names:
                # 1. Delete matching objects under the repository prefix in MinIO
                prefix = f"{repo_id}/"
                bucket.objects.filter(Prefix=prefix).delete()

                # 2. Delete the repository metadata in lakeFS
                repo.delete()
    except Exception as e:
        print(f"\n[Pytest Teardown] Error during lakeFS/MinIO test data cleanup: {e}")
