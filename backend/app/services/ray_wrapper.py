import argparse
import importlib.util
import inspect
import json
import os
import re
import sys
import time
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


def calculate_metrics(model, data_path, target_col=None) -> tuple:
    """Calculates evaluation metrics (accuracy, precision, recall, f1_score) for the model,
    and returns (metrics_dict, X_features, y_pred)."""
    import os
    import pandas as pd
    from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score

    metrics = {}
    X = None
    y_pred = None
    try:
        if not os.path.exists(data_path):
            print(f"Data path {data_path} does not exist. Skipping metrics.")
            return metrics, None, None

        # Load dataframe as CSV format
        ext = os.path.splitext(data_path.lower())[1]
        if ext != ".csv":
            raise ValueError(f"Dataset file must be a CSV file. Found extension: {ext}")

        df = pd.read_csv(data_path)
        if df.empty:
            print("Dataframe is empty. Skipping metrics.")
            return metrics, None, None

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
        metrics["dataset_samples"] = float(len(df))
        metrics["dataset_features"] = float(X.shape[1])
        
        print(f"Calculated metrics: {metrics}")
    except Exception as e:
        print(f"Error calculating metrics on the Ray side: {str(e)}")
        raise
    return metrics, X, y_pred


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
    from sklearn.linear_model import LogisticRegression, RidgeClassifier, SGDClassifier
    from sklearn.ensemble import (
        RandomForestClassifier,
        GradientBoostingClassifier,
        AdaBoostClassifier,
        ExtraTreesClassifier,
    )
    from sklearn.tree import DecisionTreeClassifier
    from sklearn.svm import SVC, LinearSVC
    from sklearn.neighbors import KNeighborsClassifier
    from sklearn.naive_bayes import GaussianNB
    from sklearn.neural_network import MLPClassifier

    estimators = {
        "logistic_regression": LogisticRegression,
        "random_forest": RandomForestClassifier,
        "decision_tree": DecisionTreeClassifier,
        "gradient_boosting": GradientBoostingClassifier,
        "svm": SVC,
        "svc": SVC,
        "linear_svc": LinearSVC,
        "knn": KNeighborsClassifier,
        "kneighbors": KNeighborsClassifier,
        "adaboost": AdaBoostClassifier,
        "extra_trees": ExtraTreesClassifier,
        "naive_bayes": GaussianNB,
        "gaussian_nb": GaussianNB,
        "sgd": SGDClassifier,
        "ridge": RidgeClassifier,
        "mlp": MLPClassifier,
    }

    try:
        from xgboost import XGBClassifier
        estimators["xgboost"] = XGBClassifier
        estimators["xgb"] = XGBClassifier
    except Exception as e:
        print(f"Notice: XGBoost not available: {e}")

    try:
        from lightgbm import LGBMClassifier
        estimators["lightgbm"] = LGBMClassifier
        estimators["lgb"] = LGBMClassifier
    except Exception as e:
        print(f"Notice: LightGBM not available: {e}")

    model_types = [m.strip() for m in model_type.split(",") if m.strip()]
    if not model_types:
        raise ValueError("model_type cannot be empty.")

    for m_type in model_types:
        if m_type not in estimators:
            raise ValueError(
                f"Unsupported model type: {m_type}. Supported types: {list(estimators.keys())}"
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

    if len(model_types) == 1:
        m_type = model_types[0]
        estimator_class = estimators[m_type]
        m_params = (
            hyperparameters.get(m_type, hyperparameters)
            if (isinstance(hyperparameters, dict) and m_type in hyperparameters and isinstance(hyperparameters[m_type], dict))
            else hyperparameters
        )
        try:
            model_instance = estimator_class(**m_params)
        except TypeError as te:
            print(f"Error instantiating {m_type} with hyperparameters {m_params}: {str(te)}. Falling back to default.")
            model_instance = estimator_class()

        pipeline = Pipeline(
            steps=[
                ("preprocessor", preprocessor),
                ("classifier", model_instance),
            ]
        )
        print(f"Fitting scikit-learn Pipeline with estimator: {m_type}...")
        pipeline.fit(X, y)
        return pipeline
    else:
        fitted_pipelines = {}
        for m_type in model_types:
            estimator_class = estimators[m_type]
            m_params = (
                hyperparameters.get(m_type, hyperparameters)
                if (isinstance(hyperparameters, dict) and m_type in hyperparameters and isinstance(hyperparameters[m_type], dict))
                else hyperparameters
            )
            try:
                model_instance = estimator_class(**m_params)
            except TypeError as te:
                print(f"Error instantiating {m_type} with hyperparameters {m_params}: {str(te)}. Falling back to default.")
                model_instance = estimator_class()

            pipeline = Pipeline(
                steps=[
                    ("preprocessor", preprocessor),
                    ("classifier", model_instance),
                ]
            )
            print(f"Fitting scikit-learn Pipeline with estimator: {m_type}...")
            pipeline.fit(X, y)
            fitted_pipelines[m_type] = pipeline
        return fitted_pipelines
def generate_run_name(model=None, model_type=None, trainer_class=None, dataset_id="", job_id=None) -> str:
    """
    Generates a concise, structured Option A (Model-First) run name:
    <model_prefix>_<dataset_id[:16]>_<job_id[:8]>
    Supports common sklearn estimators, pipelines, and custom trainer classes.
    """
    algo_map = {
        "logisticregression": "logreg",
        "randomforest": "rf",
        "randomforestclassifier": "rf",
        "randomforestregressor": "rf_reg",
        "decisiontree": "dt",
        "decisiontreeclassifier": "dt",
        "decisiontreeregressor": "dt_reg",
        "gradientboosting": "gbdt",
        "gradientboostingclassifier": "gbdt",
        "gradientboostingregressor": "gbdt_reg",
        "svc": "svc",
        "svr": "svr",
        "svm": "svm",
        "linearsvc": "linearsvc",
        "linearsvr": "linearsvr",
        "knn": "knn",
        "kneighbors": "knn",
        "kneighborsclassifier": "knn",
        "kneighborsregressor": "knn_reg",
        "adaboost": "adaboost",
        "adaboostclassifier": "adaboost",
        "adaboostregressor": "adaboost_reg",
        "extratrees": "extratrees",
        "extratreesclassifier": "extratrees",
        "extratreesregressor": "extratrees_reg",
        "gaussiannb": "gnb",
        "multinomialnb": "mnb",
        "naivebayes": "nb",
        "linearregression": "linreg",
        "ridge": "ridge",
        "ridgeclassifier": "ridge",
        "lasso": "lasso",
        "elasticnet": "elasticnet",
        "sgd": "sgd",
        "sgdclassifier": "sgd",
        "sgdregressor": "sgd_reg",
        "mlp": "mlp",
        "mlpclassifier": "mlp",
        "mlpregressor": "mlp_reg",
        "xgboost": "xgb",
        "xgb": "xgb",
        "xgbclassifier": "xgb",
        "xgbregressor": "xgb_reg",
        "lightgbm": "lgb",
        "lgb": "lgb",
        "lgbmclassifier": "lgb",
        "lgbmregressor": "lgb_reg",
    }

    raw_name = ""
    if model_type:
        raw_name = str(model_type)
    elif model is not None:
        # If it is an sklearn Pipeline, inspect the final estimator step
        if hasattr(model, "steps") and isinstance(model.steps, list) and model.steps:
            raw_name = model.steps[-1][1].__class__.__name__
        else:
            raw_name = model.__class__.__name__
    elif trainer_class is not None:
        raw_name = trainer_class.__name__
    else:
        raw_name = "model"

    clean_key = re.sub(r"[^a-zA-Z0-9]", "", raw_name).lower()
    prefix = algo_map.get(clean_key)
    if not prefix:
        snake = re.sub(r"(?<!^)(?=[A-Z])", "_", raw_name).lower()
        prefix = re.sub(r"[^a-z0-9_]", "-", snake).strip("-_")[:12] or "model"

    short_ds = dataset_id[:16].rstrip("-_") if dataset_id else "dataset"
    short_job = f"_{job_id[:8]}" if job_id else ""
    return f"{prefix}_{short_ds}{short_job}"


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
    parser.add_argument("--user", required=False, default=None)
    parser.add_argument("--job_id", required=False, default=None)
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
    training_start_time = time.time()
    trainer_class = None
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

        training_duration_seconds = round(time.time() - training_start_time, 3)
        print(f"Ray training task completed successfully in {training_duration_seconds}s.")
        print("Shutting down Ray connection...")
        ray.shutdown()
        import gc
        gc.collect()
    except Exception as e:
        print(f"Error during Ray training: {str(e)}")
        sys.exit(1)

    # 4. Register model and metadata in MLflow
    print("Registering model, parameters, metrics, and tags in MLflow...")
    try:
        tracking_uri = os.getenv("MLFLOW_TRACKING_URI", "sqlite:///mlflow.db")
        print(f"Setting MLflow tracking URI to: {tracking_uri}")
        mlflow.set_tracking_uri(tracking_uri)
        exp_name = args.experiment_name if args.experiment_name else f"dataset-{args.dataset_id}-experiment"
        mlflow.set_experiment(exp_name)

        # Detect single model vs multi-model execution
        is_multi_model = isinstance(model, dict) or (isinstance(model, list) and len(model) > 1)
        models_dict = {}
        if isinstance(model, dict):
            models_dict = model
        elif isinstance(model, list):
            for idx, m in enumerate(model):
                m_name = getattr(m, "__class__", type(m)).__name__
                models_dict[f"{m_name}_{idx+1}"] = m

        if is_multi_model:
            # =========================================================================
            # MULTI-MODEL TRAINING WORKFLOW: MLFLOW NESTED RUNS (Parent-Child Hierarchy)
            # =========================================================================
            print(f"Multi-model execution detected ({len(models_dict)} candidates). Initializing nested runs...")
            short_ds = args.dataset_id[:16].rstrip("-_") if args.dataset_id else "dataset"
            short_job = f"_{args.job_id[:8]}" if args.job_id else ""
            parent_run_name = f"multimodel_{short_ds}{short_job}"

            with mlflow.start_run(run_name=parent_run_name):
                # Log overall training tags & parameters in parent run
                mlflow.set_tags({
                    "dataset_id": str(args.dataset_id),
                    "lakefs_repo": str(repo_name),
                    "lakefs_ref": str(args.ref),
                    "pipeline_mode": str(args.pipeline_mode),
                    "mlsecops.framework": "ray-kubernetes",
                    "is_multi_model": "true",
                    "candidate_count": str(len(models_dict)),
                })
                if getattr(args, "user", None):
                    mlflow.set_tag("mlsecops.user", str(args.user))
                if getattr(args, "job_id", None):
                    mlflow.set_tag("mlsecops.job_id", str(args.job_id))

                parent_params = {
                    "candidates": ",".join(list(models_dict.keys())),
                    "candidate_count": str(len(models_dict)),
                }
                if args.target_column:
                    parent_params["target_column"] = str(args.target_column)
                if not args.pipeline_mode:
                    parent_params["epochs"] = str(args.epochs)
                mlflow.log_params(parent_params)

                champion_name = None
                champion_model = None
                champion_score = -1.0
                champion_metrics = {}
                champion_sig = None
                champion_input = None

                # Execute nested child run for each candidate model
                for cand_key, cand_model in models_dict.items():
                    child_run_name = generate_run_name(
                        model=cand_model,
                        model_type=cand_key if args.pipeline_mode else None,
                        dataset_id=args.dataset_id,
                        job_id=args.job_id,
                    )
                    print(f"Executing nested child run for candidate '{cand_key}': {child_run_name}")

                    cand_metrics, cand_X, cand_y_pred = calculate_metrics(
                        cand_model, data_path, target_col=args.target_column
                    )

                    cand_sig = None
                    cand_input = None
                    if cand_X is not None and cand_y_pred is not None:
                        try:
                            from mlflow.models.signature import infer_signature
                            cand_sig = infer_signature(cand_X, cand_y_pred)
                            if hasattr(cand_X, "iloc"):
                                cand_input = cand_X.iloc[:2]
                            elif hasattr(cand_X, "head"):
                                cand_input = cand_X.head(2)
                        except Exception:
                            pass

                    with mlflow.start_run(run_name=child_run_name, nested=True):
                        # Log candidate params
                        cand_params = {"candidate_name": str(cand_key)}
                        if isinstance(hyperparams, dict):
                            if cand_key in hyperparams and isinstance(hyperparams[cand_key], dict):
                                for hk, hv in hyperparams[cand_key].items():
                                    cand_params[str(hk)] = str(hv)[:500]
                            else:
                                for hk, hv in hyperparams.items():
                                    cand_params[str(hk)] = str(hv)[:500]
                        mlflow.log_params(cand_params)

                        # Log candidate tags
                        mlflow.set_tags({
                            "candidate_name": str(cand_key),
                            "dataset_id": str(args.dataset_id),
                            "lakefs_ref": str(args.ref),
                            "mlsecops.framework": "ray-kubernetes",
                        })

                        # Log candidate metrics
                        for m_name, m_val in cand_metrics.items():
                            mlflow.log_metric(m_name, m_val)

                        # Log candidate evaluation report
                        if cand_X is not None and cand_y_pred is not None:
                            try:
                                from sklearn.metrics import classification_report
                                import pandas as pd
                                df_cand = pd.read_csv(data_path)
                                t_col = args.target_column or ("label" if "label" in df_cand.columns else df_cand.columns[-1])
                                report_dict = classification_report(df_cand[t_col], cand_y_pred, output_dict=True, zero_division=0)
                                mlflow.log_dict(report_dict, "evaluation/classification_report.json")
                            except Exception:
                                pass

                        # Log candidate model artifact (unregistered, inside child run to avoid registry collision)
                        cand_model_kwargs = {
                            "artifact_path": "model",
                            "python_model": ModelWrapper(cand_model),
                            "pip_requirements": ["mlflow", "scikit-learn", "pandas", "cloudpickle", "xgboost", "lightgbm"],
                        }
                        if cand_sig is not None:
                            cand_model_kwargs["signature"] = cand_sig
                        if cand_input is not None:
                            cand_model_kwargs["input_example"] = cand_input
                        mlflow.pyfunc.log_model(**cand_model_kwargs)

                    # Champion selection logic (best F1-score or accuracy)
                    score = cand_metrics.get("f1_score", cand_metrics.get("accuracy", 0.0))
                    if score > champion_score or champion_model is None:
                        champion_score = score
                        champion_name = cand_key
                        champion_model = cand_model
                        champion_metrics = cand_metrics
                        champion_sig = cand_sig
                        champion_input = cand_input

                # Back in Parent Run: Record Champion Summary and Register Champion in Registry
                print(f"Champion candidate selected: '{champion_name}' with score {champion_score}")
                mlflow.set_tags({
                    "champion_candidate": str(champion_name),
                    "champion_score": str(champion_score),
                })
                for m_name, m_val in champion_metrics.items():
                    mlflow.log_metric(m_name, m_val)
                    mlflow.log_metric(f"champion_{m_name}", m_val)

                if training_duration_seconds is not None:
                    mlflow.log_metric("training_duration_seconds", float(training_duration_seconds))

                champ_log_kwargs = {
                    "artifact_path": "champion_model",
                    "python_model": ModelWrapper(champion_model),
                    "registered_model_name": args.output_model_name,
                    "pip_requirements": ["mlflow", "scikit-learn", "pandas", "cloudpickle", "xgboost", "lightgbm"],
                }
                if champion_sig is not None:
                    champ_log_kwargs["signature"] = champion_sig
                if champion_input is not None:
                    champ_log_kwargs["input_example"] = champion_input

                mlflow.pyfunc.log_model(**champ_log_kwargs)
                print(f"Champion model successfully registered under name '{args.output_model_name}' in MLflow.")

        else:
            # =========================================================================
            # SINGLE MODEL WORKFLOW
            # =========================================================================
            # Calculate evaluation metrics, features X, and predictions y_pred
            print("Calculating evaluation metrics on the Ray side...")
            metrics, X, y_pred = calculate_metrics(model, data_path, target_col=args.target_column)
            if training_duration_seconds is not None:
                metrics["training_duration_seconds"] = float(training_duration_seconds)

            # Infer model signature & extract input sample
            signature = None
            input_example = None
            if X is not None and y_pred is not None:
                try:
                    from mlflow.models.signature import infer_signature
                    signature = infer_signature(X, y_pred)
                    if hasattr(X, "iloc"):
                        input_example = X.iloc[:2]
                    elif hasattr(X, "head"):
                        input_example = X.head(2)
                    print("Successfully inferred MLflow model signature and input example.")
                except Exception as sig_err:
                    print(f"Notice: Could not infer model signature ({sig_err}). Continuing without signature.")

            # Generate Option A (Model-First) run name
            run_name = generate_run_name(
                model=model,
                model_type=args.model_type if args.pipeline_mode else None,
                trainer_class=trainer_class if not args.pipeline_mode else None,
                dataset_id=args.dataset_id,
                job_id=args.job_id,
            )
            print(f"Generated MLflow Run Name: {run_name}")

            with mlflow.start_run(run_name=run_name):
                # A. Log Parameters (Hyperparameters & Model/Execution Config)
                params_to_log = {}
                if isinstance(hyperparams, dict):
                    for k, v in hyperparams.items():
                        params_to_log[str(k)] = str(v)[:500]

                if args.pipeline_mode:
                    params_to_log["pipeline_mode"] = "true"
                    params_to_log["model_type"] = str(args.model_type)
                    if args.target_column:
                        params_to_log["target_column"] = str(args.target_column)
                else:
                    params_to_log["pipeline_mode"] = "false"
                    params_to_log["epochs"] = str(args.epochs)
                    if trainer_class:
                        params_to_log["trainer_class"] = trainer_class.__name__
                    if args.target_column:
                        params_to_log["target_column"] = str(args.target_column)

                if params_to_log:
                    print(f"Logging parameters to MLflow: {params_to_log}")
                    mlflow.log_params(params_to_log)

                # B. Log Lineage & Security Audit Tags
                tags_to_log = {
                    "dataset_id": str(args.dataset_id),
                    "lakefs_repo": str(repo_name),
                    "lakefs_ref": str(args.ref),
                    "pipeline_mode": str(args.pipeline_mode),
                    "mlsecops.framework": "ray-kubernetes",
                }
                if getattr(args, "user", None):
                    tags_to_log["mlsecops.user"] = str(args.user)
                if getattr(args, "job_id", None):
                    tags_to_log["mlsecops.job_id"] = str(args.job_id)
                if getattr(args, "code_file", None) and not args.pipeline_mode:
                    tags_to_log["code_file"] = os.path.basename(args.code_file)

                print(f"Logging tags to MLflow: {tags_to_log}")
                mlflow.set_tags(tags_to_log)

                # C. Log Metrics
                for name, val in metrics.items():
                    print(f"Logging metric to MLflow: {name}={val}")
                    mlflow.log_metric(name, val)

                # D. Log Detailed Evaluation Artifact (Classification Report)
                if X is not None and y_pred is not None:
                    try:
                        from sklearn.metrics import classification_report
                        import pandas as pd
                        df = pd.read_csv(data_path)
                        target_col = args.target_column
                        if not target_col:
                            for col in ["label", "target"]:
                                if col in df.columns:
                                    target_col = col
                                    break
                            if not target_col:
                                target_col = df.columns[-1]
                        y_true = df[target_col]
                        report_dict = classification_report(y_true, y_pred, output_dict=True, zero_division=0)
                        mlflow.log_dict(report_dict, "evaluation/classification_report.json")
                        print("Logged classification report artifact.")
                    except Exception as cr_err:
                        print(f"Notice: Could not log classification report artifact: {cr_err}")

                # E. Register Model with Signature & Input Example (Automatic environment capture)
                print("Registering model via mlflow.pyfunc with ModelWrapper...")
                log_model_kwargs = {
                    "artifact_path": "model",
                    "python_model": ModelWrapper(model),
                    "registered_model_name": args.output_model_name,
                    "pip_requirements": ["mlflow", "scikit-learn", "pandas", "cloudpickle", "xgboost", "lightgbm"],
                }
                if signature is not None:
                    log_model_kwargs["signature"] = signature
                if input_example is not None:
                    log_model_kwargs["input_example"] = input_example

                mlflow.pyfunc.log_model(**log_model_kwargs)

            print(f"Model successfully registered under name '{args.output_model_name}' in MLflow.")
    except Exception as e:
        print(f"Error registering model in MLflow: {str(e)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
