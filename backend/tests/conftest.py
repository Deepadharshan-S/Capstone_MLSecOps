import pytest
import lakefs
import boto3
from botocore.client import Config

from app.core.config import settings
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
            endpoint_url=settings.MINIO_ENDPOINT,
            aws_access_key_id=settings.MINIO_ROOT_USER,
            aws_secret_access_key=settings.MINIO_ROOT_PASSWORD,
            config=Config(signature_version='s3v4'),
            region_name='us-east-1'
        )
        bucket = s3.Bucket('lakefs')

        test_repo_names = {"iris-dataset-admin", "iris-dataset-ds", "iris-dataset-2"}

        for repo in repos:
            repo_id = repo.id
            # Identify if the repository was created by the test suite
            if (
                repo_id.startswith("test-dataset-")
                or repo_id.startswith("train-dataset-")
                or repo_id.startswith("test-repo-")
                or repo_id in test_repo_names
            ):
                # 1. Delete matching objects under the repository prefix in MinIO
                prefix = f"{repo_id}/"
                bucket.objects.filter(Prefix=prefix).delete()

                # 2. Delete the repository metadata in lakeFS
                repo.delete()
    except Exception as e:
        print(f"\n[Pytest Teardown] Error during lakeFS/MinIO test data cleanup: {e}")

    # Clean up test-specific MLflow experiments, runs, models, and MinIO artifacts
    try:
        import mlflow
        from mlflow.entities import ViewType
        from mlflow.tracking import MlflowClient
        mlflow.set_tracking_uri(settings.MLFLOW_TRACKING_URI)
        client = MlflowClient()

        # Collect experiment IDs of test experiments to delete their S3 artifacts and DB records
        test_exp_ids = []
        for exp in client.search_experiments(view_type=ViewType.ALL):
            if (
                exp.name.startswith("dataset-train-dataset-")
                or exp.name.startswith("test-")
                or exp.name.startswith("dataset-test-")
            ):
                test_exp_ids.append(exp.experiment_id)

        # 1. Delete test-registered models
        for rm in client.search_registered_models():
            if rm.name.startswith("train-dataset-") or rm.name.startswith("test-"):
                for v in rm.latest_versions:
                    client.delete_model_version(name=rm.name, version=v.version)
                client.delete_registered_model(rm.name)

        # 2. Hard-delete test experiments and runs from PostgreSQL
        if test_exp_ids:
            from sqlalchemy import create_engine, text
            
            db_url = settings.DATABASE_URL
            if db_url.endswith("/mlsecops"):
                mlflow_db_url = db_url[:-9] + "/mlflow"
            else:
                from urllib.parse import urlparse
                parsed = urlparse(db_url)
                scheme = "postgresql+psycopg" if "+psycopg" in settings.DATABASE_URL else "postgresql"
                mlflow_db_url = f"{scheme}://{parsed.username}:{parsed.password}@{parsed.hostname}:{parsed.port or 5432}/mlflow"
                
            engine = create_engine(mlflow_db_url)
            
            exp_ids_str = ", ".join([f"'{eid}'" for eid in test_exp_ids])
            
            with engine.connect() as conn:
                # Get run UUIDs to delete run-dependent records
                run_uuids_res = conn.execute(text(f"SELECT run_uuid FROM runs WHERE experiment_id IN ({exp_ids_str});"))
                run_uuids = [row[0] for row in run_uuids_res]
                
                # Get model IDs to delete model-dependent records
                model_ids_res = conn.execute(text(f"SELECT model_id FROM logged_models WHERE experiment_id IN ({exp_ids_str});"))
                model_ids = [row[0] for row in model_ids_res]
                
                # A. Delete logged_models child tables
                if model_ids:
                    model_ids_str = ", ".join([f"'{mid}'" for mid in model_ids])
                    conn.execute(text(f"DELETE FROM logged_model_metrics WHERE model_id IN ({model_ids_str});"))
                    conn.execute(text(f"DELETE FROM logged_model_params WHERE model_id IN ({model_ids_str});"))
                    conn.execute(text(f"DELETE FROM logged_model_tags WHERE model_id IN ({model_ids_str});"))
                
                # B. Delete logged_models
                conn.execute(text(f"DELETE FROM logged_models WHERE experiment_id IN ({exp_ids_str});"))
                
                # C. Delete runs child tables
                if run_uuids:
                    run_uuids_str = ", ".join([f"'{ruid}'" for ruid in run_uuids])
                    conn.execute(text(f"DELETE FROM tags WHERE run_uuid IN ({run_uuids_str});"))
                    conn.execute(text(f"DELETE FROM params WHERE run_uuid IN ({run_uuids_str});"))
                    conn.execute(text(f"DELETE FROM metrics WHERE run_uuid IN ({run_uuids_str});"))
                    conn.execute(text(f"DELETE FROM latest_metrics WHERE run_uuid IN ({run_uuids_str});"))
                    conn.execute(text(f"DELETE FROM inputs WHERE destination_id IN ({run_uuids_str}) OR source_id IN ({run_uuids_str});"))
                
                # D. Delete runs
                conn.execute(text(f"DELETE FROM runs WHERE experiment_id IN ({exp_ids_str});"))
                
                # E. Delete experiment child tables
                conn.execute(text(f"DELETE FROM experiment_tags WHERE experiment_id IN ({exp_ids_str});"))
                
                # F. Delete experiments
                conn.execute(text(f"DELETE FROM experiments WHERE experiment_id IN ({exp_ids_str});"))
                
                conn.commit()

        # 3. Delete matching objects in MinIO mlflow bucket
        if test_exp_ids:
            s3 = boto3.resource(
                's3',
                endpoint_url=settings.MINIO_ENDPOINT,
                aws_access_key_id=settings.MINIO_ROOT_USER,
                aws_secret_access_key=settings.MINIO_ROOT_PASSWORD,
                config=Config(signature_version='s3v4'),
                region_name='us-east-1'
            )
            mlflow_bucket = s3.Bucket('mlflow')
            for exp_id in test_exp_ids:
                prefix = f"{exp_id}/"
                mlflow_bucket.objects.filter(Prefix=prefix).delete()

    except Exception as e:
        print(f"\n[Pytest Teardown] Error during MLflow test data cleanup: {e}")


