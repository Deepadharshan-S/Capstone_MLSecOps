"""Eager, failure-tolerant access to the ``mlflow`` package.

mlflow's *first* import is not safe to run concurrently. Uvicorn executes these
synchronous endpoints on a threadpool, so a cold dashboard load fires
``/api/models``, ``/api/experiments`` and ``/api/deployments`` together and all
of them race to import mlflow at once. When that race loses, ``mlflow.tracking``
is left half-initialized in ``sys.modules`` and every later import in the same
process fails permanently -- restarting the server is the only recovery.

The app warms mlflow once during startup (see the lifespan in ``app.main``),
before any request is accepted, so these calls normally resolve straight from
``sys.modules`` and return immediately.

On the request path an import failure becomes a 503 rather than escaping: an
unhandled ``ImportError`` propagates *through* Starlette's ``CORSMiddleware``
before it can attach ``Access-Control-Allow-Origin``, and the browser then
reports a misleading CORS error in place of the real one.
"""

from fastapi import HTTPException, status

from app.core.config import settings


def _import_mlflow():
    """Import mlflow (and ``mlflow.pyfunc``), returning ``(mlflow, MlflowClient)``.

    Re-raises the original exception so the caller decides how to report it.
    """
    import mlflow
    import mlflow.pyfunc  # loaded here so the /predict route never races it
    from mlflow.tracking import MlflowClient

    return mlflow, MlflowClient


def warm_mlflow() -> None:
    """Import mlflow at startup, single-threaded, before requests can race it.

    Prints instead of using the logging module because the app configures no
    root logger, and this message needs to be visible in the server console.
    """
    try:
        _import_mlflow()
    except Exception as exc:
        # Swallowed on purpose: the rest of the API stays usable and the
        # mlflow-backed routes answer with a 503 from load_mlflow().
        print(
            f"[startup] mlflow import failed ({type(exc).__name__}: {exc}); "
            "mlflow-backed routes will return 503 until the server restarts."
        )
    else:
        print(f"[startup] mlflow warmed ({settings.MLFLOW_TRACKING_URI}).")


def load_mlflow():
    """Return ``(mlflow, MlflowClient)``, turning an import failure into a 503."""
    try:
        return _import_mlflow()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "MLflow could not be imported, so the MLflow client is unavailable. "
                "Restart the API server to rebuild it. "
                f"({type(exc).__name__}: {exc})"
            ),
        ) from exc
