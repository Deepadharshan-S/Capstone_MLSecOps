import os
import time
import mlflow
import mlflow.pyfunc
import pandas as pd
from fastapi import FastAPI, Request, HTTPException
from ray import serve

# Graceful OpenTelemetry imports (safe fallback if packages are not present)
try:
    from opentelemetry import trace, propagate
    from opentelemetry.trace import StatusCode, SpanKind
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    OTEL_AVAILABLE = True
except ImportError:
    OTEL_AVAILABLE = False
    trace = None
    propagate = None
    StatusCode = None
    SpanKind = None

app = FastAPI(title="SentinelML Model Serving")


@serve.deployment(num_replicas=1, ray_actor_options={"num_cpus": 0.5})
@serve.ingress(app)
class ModelServingDeployment:
    def __init__(self, tracer_provider=None):
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

        # Initialize OpenTelemetry for RayService distributed tracing
        self.tracer = None
        self.tracer_provider = None
        otel_enabled = os.getenv("OTEL_ENABLED", "false").lower() in ("true", "1", "yes")

        if OTEL_AVAILABLE and (otel_enabled or tracer_provider is not None):
            try:
                if tracer_provider is not None:
                    self.tracer_provider = tracer_provider
                else:
                    service_name = os.getenv("OTEL_SERVICE_NAME", "sentinelml-rayservice")
                    service_version = os.getenv("OTEL_SERVICE_VERSION", "0.1.0")
                    environment = os.getenv("ENVIRONMENT", "development")
                    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://host.docker.internal:4318")
                    timeout = float(os.getenv("OTEL_EXPORTER_OTLP_TIMEOUT_SECONDS", "5"))

                    resource = Resource.create({
                        "service.name": service_name,
                        "service.version": service_version,
                        "deployment.environment": environment,
                    })

                    traces_endpoint = endpoint.rstrip("/")
                    if not traces_endpoint.endswith("/v1/traces"):
                        traces_endpoint = f"{traces_endpoint}/v1/traces"

                    exporter = OTLPSpanExporter(endpoint=traces_endpoint, timeout=timeout)
                    tp = TracerProvider(resource=resource)
                    tp.add_span_processor(BatchSpanProcessor(exporter))
                    self.tracer_provider = tp

                self.tracer = self.tracer_provider.get_tracer("sentinelml.rayservice")
                print("OpenTelemetry distributed tracing initialized for RayService.")
            except Exception as otel_err:
                print(f"Notice: Could not initialize OpenTelemetry in RayService ({otel_err}). Serving will continue without tracing.")
                self.tracer = None

    @app.get("/healthz")
    def healthz(self):
        return {
            "status": "healthy",
            "model_loaded": self.model is not None,
            "timestamp": time.time(),
        }

    async def _perform_inference(self, request: Request) -> dict:
        """Executes model inference without exposing sensitive payloads or headers."""
        if self.model is None:
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
        except HTTPException:
            raise
        except Exception as pred_err:
            raise HTTPException(status_code=400, detail=f"Inference error: {str(pred_err)}")

    @app.post("/predict")
    async def predict(self, request: Request):
        # Extract incoming W3C trace context from HTTP headers
        carrier = dict(request.headers)
        ctx = propagate.extract(carrier) if (OTEL_AVAILABLE and propagate is not None) else None

        if self.tracer is not None:
            span_attrs = {
                "ml.operation": "model.inference",
                "ml.serving.target": "rayservice",
            }
            model_name = os.getenv("MODEL_NAME")
            if model_name:
                span_attrs["ml.model.name"] = str(model_name)

            with self.tracer.start_as_current_span(
                "rayserve.inference",
                context=ctx,
                kind=SpanKind.SERVER,
                attributes=span_attrs,
            ) as span:
                try:
                    result = await self._perform_inference(request)
                    span.set_status(StatusCode.OK)
                    return result
                except HTTPException as http_exc:
                    span.record_exception(http_exc)
                    span.set_status(StatusCode.ERROR)
                    raise
                except Exception as exc:
                    span.record_exception(exc)
                    span.set_status(StatusCode.ERROR)
                    raise
        else:
            return await self._perform_inference(request)


entrypoint = ModelServingDeployment.bind()
