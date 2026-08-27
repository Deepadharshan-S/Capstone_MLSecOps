import argparse
import importlib.util
import inspect
import json
import os
import re
import sys
import mlflow
import mlflow.pyfunc
import ray

class ModelWrapper(mlflow.pyfunc.PythonModel):
    """Generic MLflow Model wrapper that can pickle any custom Python model object."""
    def __init__(self, model):
        self.model = model

    def predict(self, context, model_input):
        if hasattr(self.model, "predict"):
            return self.model.predict(model_input)
        return self.model


def get_repo_name(name: str) -> str:
    """Sanitizes a dataset name to make it a valid lakeFS repository name."""
    sanitized = name.lower()
    sanitized = re.sub(r"[^a-z0-9-]", "-", sanitized)
    sanitized = re.sub(r"-+", "-", sanitized)
    sanitized = sanitized.strip("-")
    if len(sanitized) < 3:
        sanitized = (sanitized + "repo")[:3]
    if len(sanitized) > 63:
        sanitized = sanitized[:63].rstrip("-")
    return sanitized


def download_dataset(repo_name: str, ref_id: str, dest_dir: str) -> str:
    """Downloads all objects from lakeFS at the specified ref to dest_dir."""
    import lakefs
    os.makedirs(dest_dir, exist_ok=True)

    client = lakefs.Client(
        username=os.getenv("LAKEFS_ACCESS_KEY_ID"),
        password=os.getenv("LAKEFS_SECRET_ACCESS_KEY"),
        host=os.getenv("LAKEFS_ENDPOINT"),
    )

    repo = lakefs.Repository(repo_name, client=client)
    ref = repo.ref(ref_id)

    downloaded_files = []
    for obj in ref.objects():
        local_path = os.path.join(dest_dir, obj.path)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)

        with ref.object(obj.path).reader(mode="rb") as reader:
            content = reader.read()

        with open(local_path, "wb") as f:
            f.write(content)

        downloaded_files.append(local_path)

    if not downloaded_files:
        raise ValueError(f"No files found in lakeFS repository '{repo_name}' at reference '{ref_id}'")

    return downloaded_files[0]


def execute_training_task(trainer_class, data_path, epochs, hyperparameters):
    """Ray task executing the train function from the user's class."""
    trainer_instance = trainer_class()
    model = trainer_instance.train(data_path, epochs, **hyperparameters)
    return model


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset_id", required=True)
    parser.add_argument("--ref", default="main")
    parser.add_argument("--epochs", type=int, default=10)
    parser.add_argument("--hyperparameters", default="{}")
    parser.add_argument("--code_file", required=True)
    parser.add_argument("--output_model_name", required=True)
    parser.add_argument("--job_dir", required=True)
    args = parser.parse_args()

    # Parse hyperparameters
    try:
        hyperparams = json.loads(args.hyperparameters)
    except Exception:
        hyperparams = {}

    # 1. Download dataset from lakeFS
    repo_name = get_repo_name(args.dataset_id)
    dest_dir = os.path.join(args.job_dir, "data")
    print(f"Downloading dataset from lakeFS repository '{repo_name}' at ref '{args.ref}'...")
    try:
        data_path = download_dataset(repo_name, args.ref, dest_dir)
        print(f"Dataset downloaded successfully to: {data_path}")
    except Exception as e:
        print(f"Error downloading dataset from lakeFS: {str(e)}")
        sys.exit(1)

    # 2. Initialize Ray
    print("Initializing Ray...")
    ray.init(ignore_reinit_error=True)

    # 3. Load user class code
    print(f"Loading user code from: {args.code_file}")
    try:
        spec = importlib.util.spec_from_file_location("user_code", args.code_file)
        user_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(user_module)
    except Exception as e:
        print(f"Error loading user training code: {str(e)}")
        sys.exit(1)

    # Find the class containing a train method
    trainer_class = None
    for name, obj in inspect.getmembers(user_module, inspect.isclass):
        if hasattr(obj, "train") and callable(getattr(obj, "train")):
            trainer_class = obj
            break

    if not trainer_class:
        print("Error: Could not find any class with a callable 'train' method in user code.")
        sys.exit(1)

    print(f"Found trainer class: '{trainer_class.__name__}'. Submitting task to Ray...")

    # 4. Run Ray task
    try:
        # Execute the training task locally (local_mode equivalent to save memory)
        model = execute_training_task(
            trainer_class, data_path, args.epochs, hyperparams
        )
        print("Ray training task completed successfully.")
        print("Shutting down Ray connection...")
        ray.shutdown()
        import gc
        gc.collect()
    except Exception as e:
        print(f"Error during Ray training: {str(e)}")
        sys.exit(1)

    # 5. Register model in MLflow Model Registry
    print("Registering model in MLflow...")
    try:
        tracking_uri = os.getenv("MLFLOW_TRACKING_URI", "sqlite:///mlflow.db")
        print(f"Setting MLflow tracking URI to: {tracking_uri}")
        mlflow.set_tracking_uri(tracking_uri)
        mlflow.set_experiment(f"dataset-{args.dataset_id}-experiment")

        with mlflow.start_run() as run:
            print("Registering model via mlflow.pyfunc with ModelWrapper...")
            mlflow.pyfunc.log_model(
                artifact_path="model",
                python_model=ModelWrapper(model),
                registered_model_name=args.output_model_name,
                pip_requirements=["mlflow", "scikit-learn", "pandas", "cloudpickle"]
            )
        print(f"Model successfully registered under name '{args.output_model_name}' in MLflow.")
    except Exception as e:
        print(f"Error registering model in MLflow: {str(e)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
