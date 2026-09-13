import os
import sys
import time
import ray
from ray import serve
from fastapi import FastAPI, Request, HTTPException
import mlflow.pyfunc
import pandas as pd

app = FastAPI(title="MLSecOps Model Serving")


@serve.deployment(num_replicas=1, ray_actor_options={"num_cpus": 0.5})
@serve.ingress(app)
class ModelServingDeployment:
    def __init__(self):
        tracking_uri = os.getenv("MLFLOW_TRACKING_URI", "http://host.docker.internal:5000")
        mlflow.set_tracking_uri(tracking_uri)
        model_uri = os.getenv("MODEL_URI")
        print(f"Initializing ModelServingDeployment with MODEL_URI={model_uri}, TRACKING_URI={tracking_uri}")
        if not model_uri:
            print("Warning: MODEL_URI environment variable is not set.")
            self.model = None
        else:
            try:
                self.model = mlflow.pyfunc.load_model(model_uri)
                print(f"Successfully loaded MLflow model from: {model_uri}")
            except Exception as e:
                print(f"Notice: Could not load model from MLflow ({e}). Initializing fallback.")
                self.model = None

    @app.get("/healthz")
    def healthz(self):
        return {
            "status": "healthy",
            "model_loaded": self.model is not None,
            "timestamp": time.time(),
        }

    @app.post("/predict")
    async def predict(self, request: Request):
        if self.model is None:
            # Re-attempt lazy loading if model_uri is available
            model_uri = os.getenv("MODEL_URI")
            if model_uri:
                try:
                    self.model = mlflow.pyfunc.load_model(model_uri)
                except Exception as load_err:
                    raise HTTPException(
                        status_code=503,
                        detail=f"Model failed to load from {model_uri}: {str(load_err)}",
                    )
            else:
                raise HTTPException(status_code=503, detail="Model is not loaded on this serving instance.")

        payload = await request.json()

        try:
            if "dataframe_records" in payload and payload["dataframe_records"] is not None:
                df = pd.DataFrame(payload["dataframe_records"])
            elif "inputs" in payload and payload["inputs"] is not None:
                df = pd.DataFrame(payload["inputs"])
            elif isinstance(payload, list):
                df = pd.DataFrame(payload)
            elif isinstance(payload, dict):
                if "columns" in payload and "data" in payload:
                    df = pd.DataFrame(data=payload["data"], columns=payload["columns"])
                else:
                    df = pd.DataFrame([payload])
            else:
                raise ValueError("Payload must contain 'dataframe_records' or 'inputs'.")

            preds = self.model.predict(df)
            if hasattr(preds, "tolist"):
                preds = preds.tolist()
            elif hasattr(preds, "to_dict"):
                preds = preds.to_dict()

            return {"predictions": preds, "status": "success"}
        except Exception as pred_err:
            raise HTTPException(status_code=400, detail=f"Inference error: {str(pred_err)}")


entrypoint = ModelServingDeployment.bind()
