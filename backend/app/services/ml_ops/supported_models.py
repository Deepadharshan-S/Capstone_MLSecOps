"""Canonical list of model types supported by the automated pipeline.

Single source of truth — mirrors the ``estimators`` dict in
``app/services/ml_ops/ray_wrapper.py::execute_pipeline_task``.
``TrainPipelineSchema`` validates against this, ``GET /models/supported``
exposes it to the frontend, and the frontend model picker is built from it.
"""

# Always available (scikit-learn core, installed in Dockerfile.ray)
CORE_MODEL_TYPES = [
    "logistic_regression",
    "random_forest",
    "decision_tree",
    "gradient_boosting",
    "svm",
    "knn",
    "adaboost",
    "extra_trees",
    "naive_bayes",
    "sgd",
    "ridge",
    "mlp",
]

# Aliases accepted by ray_wrapper (canonical name first)
MODEL_ALIASES = {
    "logistic_regression": ["logistic_regression"],
    "random_forest": ["random_forest"],
    "decision_tree": ["decision_tree"],
    "gradient_boosting": ["gradient_boosting"],
    "svm": ["svm", "svc", "linear_svc"],
    "knn": ["knn", "kneighbors"],
    "adaboost": ["adaboost"],
    "extra_trees": ["extra_trees"],
    "naive_bayes": ["naive_bayes", "gaussian_nb"],
    "sgd": ["sgd"],
    "ridge": ["ridge"],
    "mlp": ["mlp"],
    "xgboost": ["xgboost", "xgb"],
    "lightgbm": ["lightgbm", "lgb"],
}

# Optional deps — present in Dockerfile.ray but may be missing locally
OPTIONAL_MODEL_TYPES = ["xgboost", "lightgbm"]

# Every alias ray_wrapper accepts (used for request validation)
ALL_ACCEPTED_TYPES = sorted({a for aliases in MODEL_ALIASES.values() for a in aliases})

# Canonical names the UI should offer (core + optional flagged separately)
CANONICAL_MODEL_TYPES = CORE_MODEL_TYPES + OPTIONAL_MODEL_TYPES


def normalize_model_type(raw: str) -> str:
    """Map an alias (e.g. 'svc', 'xgb') to its canonical name."""
    key = (raw or "").strip().lower()
    for canonical, aliases in MODEL_ALIASES.items():
        if key in aliases:
            return canonical
    return key


def parse_model_list(model_type: str) -> list[str]:
    """Split a comma-separated model_type into normalized canonical names."""
    return [normalize_model_type(m) for m in (model_type or "").split(",") if m.strip()]
