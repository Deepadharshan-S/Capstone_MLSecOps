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


def calculate_metrics(model, data_path, target_col=None) -> dict:
    """Calculates evaluation metrics (accuracy, precision, recall, f1_score) for the model."""
    import os
    import pandas as pd
    from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score

    metrics = {}
    try:
        if not os.path.exists(data_path):
            print(f"Data path {data_path} does not exist. Skipping metrics.")
            return metrics

        # Load dataframe as CSV format
        ext = os.path.splitext(data_path.lower())[1]
        if ext != ".csv":
            raise ValueError(f"Dataset file must be a CSV file. Found extension: {ext}")

        df = pd.read_csv(data_path)
        if df.empty:
            print("Dataframe is empty. Skipping metrics.")
            return metrics

        if target_col is None:
            for col in ["label", "target"]:
                if col in df.columns:
                    target_col = col
                    break
            if target_col is None:
                target_col = df.columns[-1]

        if target_col not in df.columns:
            raise ValueError(f"Target column '{target_col}' not found in dataset columns: {list(df.columns)}")

        y = df[target_col]
        
        # Determine features X
        if hasattr(model, "feature_names_in_"):
            X = df[model.feature_names_in_]
        else:
            X = df.drop(columns=[target_col])

        y_pred = model.predict(X)

        # Classification metrics
        metrics["accuracy"] = float(accuracy_score(y, y_pred))
        metrics["precision"] = float(precision_score(y, y_pred, average="weighted", zero_division=0))
        metrics["recall"] = float(recall_score(y, y_pred, average="weighted", zero_division=0))
        metrics["f1_score"] = float(f1_score(y, y_pred, average="weighted", zero_division=0))
        
        print(f"Calculated metrics: {metrics}")
    except Exception as e:
        print(f"Error calculating metrics on the Ray side: {str(e)}")
        raise
    return metrics


def execute_training_task(trainer_class, data_path, epochs, hyperparameters):
    """Ray task executing the train function from the user's class."""
    trainer_instance = trainer_class()
    model = trainer_instance.train(data_path, epochs, **hyperparameters)
    return model


def execute_pipeline_task(data_path, target_column, model_type, hyperparameters):
    """Executes generic preprocessing and trains an sklearn estimator on the data."""
    import pandas as pd
    from sklearn.compose import ColumnTransformer
    from sklearn.pipeline import Pipeline
    from sklearn.impute import SimpleImputer
    from sklearn.preprocessing import StandardScaler, OneHotEncoder
    
    # Supported sklearn model estimators
    from sklearn.linear_model import LogisticRegression
    from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
    from sklearn.tree import DecisionTreeClassifier
    from sklearn.svm import SVC

    estimators = {
        "logistic_regression": LogisticRegression,
        "random_forest": RandomForestClassifier,
        "decision_tree": DecisionTreeClassifier,
        "gradient_boosting": GradientBoostingClassifier,
        "svm": SVC,
    }

    if model_type not in estimators:
        raise ValueError(
            f"Unsupported model type: {model_type}. Supported types: {list(estimators.keys())}"
        )

    # Load data
    df = pd.read_csv(data_path)
    if target_column not in df.columns:
        raise ValueError(f"Target column '{target_column}' not found in dataset columns: {list(df.columns)}")

    X = df.drop(columns=[target_column])
    y = df[target_column]

    # Automatic column typing
    numeric_cols = X.select_dtypes(include=["int64", "float64"]).columns.tolist()
    categorical_cols = X.select_dtypes(include=["object", "category", "bool"]).columns.tolist()

    # Preprocessing pipelines
    numeric_transformer = Pipeline(
        steps=[
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler()),
        ]
    )

    categorical_transformer = Pipeline(
        steps=[
            ("imputer", SimpleImputer(strategy="most_frequent")),
            ("onehot", OneHotEncoder(handle_unknown="ignore", sparse_output=False)),
        ]
    )

    preprocessor = ColumnTransformer(
        transformers=[
            ("num", numeric_transformer, numeric_cols),
            ("cat", categorical_transformer, categorical_cols),
        ]
    )

    # Initialize model estimator
    estimator_class = estimators[model_type]
    
    # Instantiate with user hyperparameters if provided, otherwise default
    try:
        model_instance = estimator_class(**hyperparameters)
    except TypeError as te:
        print(f"Error instantiating {model_type} with hyperparameters {hyperparameters}: {str(te)}")
        print("Falling back to default hyperparameters.")
        model_instance = estimator_class()

    # Define final sklearn Pipeline
    pipeline = Pipeline(
        steps=[
            ("preprocessor", preprocessor),
            ("classifier", model_instance),
        ]
    )

    # Fit pipeline
    print(f"Fitting scikit-learn Pipeline with estimator: {model_type}...")
    pipeline.fit(X, y)
    return pipeline



def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset_id", required=True)
    parser.add_argument("--ref", default="main")
    parser.add_argument("--epochs", type=int, default=10)
    parser.add_argument("--hyperparameters", default="{}")
    parser.add_argument("--code_file", required=False)
    parser.add_argument("--pipeline_mode", action="store_true")
    parser.add_argument("--target_column", required=False)
    parser.add_argument("--model_type", required=False)
    parser.add_argument("--output_model_name", required=True)
    parser.add_argument("--job_dir", required=True)
    parser.add_argument("--experiment_name", required=False)
    args = parser.parse_args()

    # Validate custom code vs pipeline mode arguments
    if not args.pipeline_mode and not args.code_file:
        parser.error("--code_file is required when not running in --pipeline_mode")
    if args.pipeline_mode and not args.target_column:
        parser.error("--target_column is required in --pipeline_mode")
    if args.pipeline_mode and not args.model_type:
        parser.error("--model_type is required in --pipeline_mode")

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
        
        # Ensure that the dataset is in CSV format
        ext = os.path.splitext(data_path.lower())[1]
        if ext != ".csv":
            raise ValueError(f"Dataset file must be a CSV file. Found extension: {ext}")
    except Exception as e:
        print(f"Error downloading dataset from lakeFS: {str(e)}")
        sys.exit(1)

    # 2. Initialize Ray
    print("Initializing Ray...")
    ray.init(ignore_reinit_error=True)

    # 3. Execute training task
    try:
        if args.pipeline_mode:
            print("Running in pipeline mode. Initiating automated preprocessing and model fitting...")
            model = execute_pipeline_task(
                data_path, args.target_column, args.model_type, hyperparams
            )
        else:
            # Load user class code
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
        exp_name = args.experiment_name if args.experiment_name else f"dataset-{args.dataset_id}-experiment"
        mlflow.set_experiment(exp_name)

        with mlflow.start_run() as run:
            print("Registering model via mlflow.pyfunc with ModelWrapper...")
            mlflow.pyfunc.log_model(
                artifact_path="model",
                python_model=ModelWrapper(model),
                registered_model_name=args.output_model_name,
                pip_requirements=["mlflow", "scikit-learn", "pandas", "cloudpickle"]
            )
            
            # Calculate and log metrics
            print("Calculating evaluation metrics on the Ray side...")
            metrics = calculate_metrics(model, data_path, target_col=args.target_column)
            for name, val in metrics.items():
                print(f"Logging metric to MLflow: {name}={val}")
                mlflow.log_metric(name, val)
        print(f"Model successfully registered under name '{args.output_model_name}' in MLflow.")
    except Exception as e:
        print(f"Error registering model in MLflow: {str(e)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
