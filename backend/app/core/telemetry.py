import logging
from contextlib import contextmanager
from typing import Optional

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider, SpanProcessor
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import (
    ALWAYS_OFF,
    ALWAYS_ON,
    ParentBased,
    Sampler,
    TraceIdRatioBased,
)

from app.core.config import Settings, settings as app_settings

logger = logging.getLogger("telemetry")

_initialized: bool = False
_tracer_provider: Optional[TracerProvider] = None
_meter_provider: Optional[MeterProvider] = None


def get_sampler(sampler_name: Optional[str], arg: float = 1.0) -> Sampler:
    """
    Resolves standard OpenTelemetry samplers from configuration string.
    Supported values: 'always_on', 'always_off', 'traceidratio',
    'parentbased_always_on', 'parentbased_always_off', 'parentbased_traceidratio'.
    Defaults to ALWAYS_ON if unrecognized.
    """
    name = (sampler_name or "always_on").lower().strip()
    if name == "always_on":
        return ALWAYS_ON
    elif name == "always_off":
        return ALWAYS_OFF
    elif name in ("traceidratio", "trace_id_ratio"):
        return TraceIdRatioBased(arg)
    elif name in ("parentbased_always_on", "parentbased"):
        return ParentBased(ALWAYS_ON)
    elif name in ("parentbased_always_off",):
        return ParentBased(ALWAYS_OFF)
    elif name in ("parentbased_traceidratio", "parentbased_trace_id_ratio"):
        return ParentBased(TraceIdRatioBased(arg))
    else:
        logger.warning(f"Unknown OTel sampler '{sampler_name}', defaulting to 'always_on'.")
        return ALWAYS_ON


def normalize_traces_endpoint(base_endpoint: str) -> str:
    """Ensures traces endpoint points to the standard OTLP /v1/traces path."""
    cleaned = base_endpoint.rstrip("/")
    if cleaned.endswith("/v1/traces"):
        return cleaned
    return f"{cleaned}/v1/traces"


def normalize_metrics_endpoint(base_endpoint: str) -> str:
    """Ensures metrics endpoint points to the standard OTLP /v1/metrics path."""
    cleaned = base_endpoint.rstrip("/")
    if cleaned.endswith("/v1/metrics"):
        return cleaned
    return f"{cleaned}/v1/metrics"


def build_resource(
    service_name: str,
    service_version: str,
    environment: str,
    additional_attributes: Optional[dict] = None,
) -> Resource:
    """Builds the standard OpenTelemetry Resource descriptor for SentinelML."""
    attributes = {
        "service.name": service_name,
        "service.version": service_version,
        "deployment.environment": environment,
    }
    if additional_attributes:
        attributes.update(additional_attributes)
    return Resource.create(attributes)


def initialize_telemetry(
    cfg: Optional[Settings] = None,
    trace_exporter: Optional[SpanExporter] = None,
    span_processor: Optional[SpanProcessor] = None,
    metric_reader: Optional[MetricReader] = None,
) -> bool:
    """
    Initializes the OpenTelemetry TracerProvider and MeterProvider for SentinelML.

    Guarantees:
    - Safe & Idempotent: If OTEL_ENABLED is False, returns False immediately.
    - Non-blocking: Telemetry uses BatchSpanProcessor and PeriodicExportingMetricReader
      with asynchronous background workers. If the collector is unavailable,
      FastAPI startup and request handling continue uninterrupted.
    - Extensible: Supports dependency injection of custom exporters for unit testing.
    """
    global _initialized, _tracer_provider, _meter_provider

    active_settings = cfg or app_settings

    if not active_settings.OTEL_ENABLED:
        logger.info("OpenTelemetry is disabled (OTEL_ENABLED=False).")
        return False

    if _initialized:
        logger.debug("OpenTelemetry is already initialized.")
        return True

    try:
        service_name = active_settings.OTEL_SERVICE_NAME or "sentinelml-backend"
        service_version = active_settings.OTEL_SERVICE_VERSION or active_settings.APP_VERSION
        environment = active_settings.ENVIRONMENT or "development"

        resource = build_resource(
            service_name=service_name,
            service_version=service_version,
            environment=environment,
        )

        sampler = get_sampler(
            active_settings.OTEL_TRACES_SAMPLER,
            active_settings.OTEL_TRACES_SAMPLER_ARG,
        )

        # 1. Tracing Configuration
        tp = TracerProvider(resource=resource, sampler=sampler)
        if span_processor is not None:
            tp.add_span_processor(span_processor)
        else:
            if trace_exporter is None:
                traces_url = normalize_traces_endpoint(active_settings.OTEL_EXPORTER_OTLP_ENDPOINT)
                trace_exporter = OTLPSpanExporter(
                    endpoint=traces_url,
                    timeout=active_settings.OTEL_EXPORTER_OTLP_TIMEOUT_SECONDS,
                )
            tp.add_span_processor(BatchSpanProcessor(trace_exporter))

        try:
            trace.set_tracer_provider(tp)
        except Exception as set_err:
            logger.debug(f"TracerProvider already registered globally: {set_err}")
        _tracer_provider = tp

        # 2. Metrics Configuration
        if metric_reader is None:
            metrics_url = normalize_metrics_endpoint(active_settings.OTEL_EXPORTER_OTLP_ENDPOINT)
            m_exporter = OTLPMetricExporter(
                endpoint=metrics_url,
                timeout=active_settings.OTEL_EXPORTER_OTLP_TIMEOUT_SECONDS,
            )
            metric_reader = PeriodicExportingMetricReader(
                m_exporter,
                export_interval_millis=active_settings.OTEL_METRICS_EXPORT_INTERVAL_MS,
            )

        mp = MeterProvider(resource=resource, metric_readers=[metric_reader])
        try:
            metrics.set_meter_provider(mp)
        except Exception as set_err:
            logger.debug(f"MeterProvider already registered globally: {set_err}")
        _meter_provider = mp

        _initialized = True
        logger.info(
            f"OpenTelemetry initialized for service '{service_name}' ({service_version}) "
            f"targeting {active_settings.OTEL_EXPORTER_OTLP_ENDPOINT}."
        )
        return True

    except Exception as exc:
        logger.warning(f"Failed to initialize OpenTelemetry ({exc}). Continuing without telemetry.")
        return False


def shutdown_telemetry() -> None:
    """
    Gracefully flushes and shuts down OpenTelemetry providers.
    Safe to call when telemetry is uninitialized or disabled.
    """
    global _initialized, _tracer_provider, _meter_provider

    if not _initialized:
        return

    try:
        if _tracer_provider is not None:
            logger.debug("Shutting down OpenTelemetry TracerProvider...")
            _tracer_provider.shutdown()
        if _meter_provider is not None:
            logger.debug("Shutting down OpenTelemetry MeterProvider...")
            _meter_provider.shutdown()
    except Exception as exc:
        logger.warning(f"Error during OpenTelemetry shutdown: {exc}")
    finally:
        _initialized = False
        _tracer_provider = None
        _meter_provider = None
        _ml_instruments.clear()
        logger.info("OpenTelemetry shutdown completed.")


def flush_telemetry(timeout_millis: int = 5000) -> bool:
    """
    Forces an immediate flush of pending spans and metrics to the configured exporters.
    Returns True if both providers flushed successfully.
    """
    success = True
    if _tracer_provider is not None:
        try:
            tracer_flushed = _tracer_provider.force_flush(timeout_millis=timeout_millis)
            success = bool(tracer_flushed) and success
        except Exception as exc:
            logger.warning(f"Error flushing TracerProvider: {exc}")
            success = False
    if _meter_provider is not None:
        try:
            meter_flushed = _meter_provider.force_flush(timeout_millis=timeout_millis)
            success = bool(meter_flushed) and success
        except Exception as exc:
            logger.warning(f"Error flushing MeterProvider: {exc}")
            success = False
    return success


def is_telemetry_enabled() -> bool:
    """Returns True if telemetry was successfully initialized and is active."""
    return _initialized


def get_tracer(name: str, version: Optional[str] = None):
    """
    Retrieves a named Tracer.
    Returns standard Tracer (or no-op if uninitialized).
    """
    if _tracer_provider is not None:
        return _tracer_provider.get_tracer(name, version)
    return trace.get_tracer(name, version)


def get_meter(name: str, version: Optional[str] = None):
    """
    Retrieves a named Meter.
    Returns standard Meter (or no-op if uninitialized).
    """
    if _meter_provider is not None:
        return _meter_provider.get_meter(name, version)
    return metrics.get_meter(name, version)


def instrument_fastapi_app(app, cfg: Optional[Settings] = None) -> bool:
    """
    Instruments a FastAPI application with OpenTelemetry automatic HTTP tracing and metrics.
    Only instruments if OTEL_ENABLED is True and the app is not already instrumented.
    """
    active_settings = cfg or app_settings
    if not active_settings.OTEL_ENABLED:
        logger.debug("FastAPI OpenTelemetry instrumentation skipped (OTEL_ENABLED=False).")
        return False

    # Idempotency check: don't instrument multiple times
    if getattr(app, "_is_instrumented_by_opentelemetry", False):
        logger.debug("FastAPI application is already instrumented with OpenTelemetry.")
        return True

    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        excluded_urls = active_settings.OTEL_EXCLUDED_URLS
        FastAPIInstrumentor().instrument_app(
            app,
            tracer_provider=_tracer_provider,
            meter_provider=_meter_provider,
            excluded_urls=excluded_urls,
            exclude_spans=["receive", "send"],
        )
        if hasattr(app, "middleware_stack"):
            app.middleware_stack = app.build_middleware_stack()

        logger.info(
            f"FastAPI application instrumented with OpenTelemetry (excluded_urls='{excluded_urls}')."
        )
        return True
    except Exception as exc:
        logger.warning(f"Failed to instrument FastAPI application with OpenTelemetry: {exc}")
        return False


def uninstrument_fastapi_app(app) -> None:
    """Safely uninstruments a FastAPI application."""
    if not getattr(app, "_is_instrumented_by_opentelemetry", False):
        return

    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor().uninstrument_app(app)
        if hasattr(app, "middleware_stack"):
            app.middleware_stack = app.build_middleware_stack()
        logger.debug("FastAPI application uninstrumented.")
    except Exception as exc:
        logger.debug(f"Notice during FastAPI uninstrumentation: {exc}")


def instrument_sqlalchemy_engine(
    engine,
    cfg: Optional[Settings] = None,
    tracer_provider: Optional[TracerProvider] = None,
    meter_provider: Optional[MeterProvider] = None,
) -> bool:
    """
    Instruments a SQLAlchemy engine instance with OpenTelemetry tracing.
    Only instruments if OTEL_ENABLED is True and the engine is not already instrumented.
    Parameter values are never captured to protect data privacy.
    """
    active_settings = cfg or app_settings
    if not active_settings.OTEL_ENABLED:
        logger.debug("SQLAlchemy OpenTelemetry instrumentation skipped (OTEL_ENABLED=False).")
        return False

    if getattr(engine, "_otel_instrumented", False):
        logger.debug("SQLAlchemy engine is already instrumented with OpenTelemetry.")
        return True

    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        tp = tracer_provider or _tracer_provider
        mp = meter_provider or _meter_provider
        SQLAlchemyInstrumentor().instrument(
            engine=engine,
            tracer_provider=tp,
            meter_provider=mp,
            enable_commenter=False,
        )
        engine._otel_instrumented = True
        logger.info(f"SQLAlchemy engine '{engine.name}' instrumented with OpenTelemetry.")
        return True
    except Exception as exc:
        logger.warning(f"Failed to instrument SQLAlchemy engine with OpenTelemetry: {exc}")
        return False


def uninstrument_sqlalchemy_engine(engine=None) -> None:
    """Safely uninstruments a SQLAlchemy engine."""
    if engine is not None and not getattr(engine, "_otel_instrumented", False):
        return

    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        SQLAlchemyInstrumentor().uninstrument()
        logger.debug("SQLAlchemy engine uninstrumented.")
    except Exception as exc:
        logger.debug(f"Notice during SQLAlchemy uninstrumentation: {exc}")
    finally:
        if engine is not None:
            engine._otel_instrumented = False


_httpx_instrumented: bool = False


def instrument_httpx(
    cfg: Optional[Settings] = None,
    tracer_provider: Optional[TracerProvider] = None,
    meter_provider: Optional[MeterProvider] = None,
) -> bool:
    """
    Globally instruments HTTPX Client and AsyncClient transports with OpenTelemetry tracing.
    Enables automatic W3C traceparent propagation to downstream services (e.g. RayService).
    Never captures request/response bodies or sensitive headers.
    """
    global _httpx_instrumented
    active_settings = cfg or app_settings
    if not active_settings.OTEL_ENABLED:
        logger.debug("HTTPX OpenTelemetry instrumentation skipped (OTEL_ENABLED=False).")
        return False

    if _httpx_instrumented:
        logger.debug("HTTPX is already instrumented with OpenTelemetry.")
        return True

    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        tp = tracer_provider or _tracer_provider
        mp = meter_provider or _meter_provider
        HTTPXClientInstrumentor().instrument(
            tracer_provider=tp,
            meter_provider=mp,
        )
        _httpx_instrumented = True
        logger.info("HTTPX clients globally instrumented with OpenTelemetry.")
        return True
    except Exception as exc:
        logger.warning(f"Failed to instrument HTTPX with OpenTelemetry: {exc}")
        return False


def uninstrument_httpx() -> None:
    """Safely uninstruments HTTPX clients."""
    global _httpx_instrumented
    if not _httpx_instrumented:
        return

    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().uninstrument()
        logger.debug("HTTPX clients uninstrumented.")
    except Exception as exc:
        logger.debug(f"Notice during HTTPX uninstrumentation: {exc}")
    finally:
        _httpx_instrumented = False


def _reset_telemetry_state(app=None, engine=None) -> None:
    """Internal test helper to reset module state between test cases."""
    global _initialized, _tracer_provider, _meter_provider, _httpx_instrumented
    if app is not None:
        uninstrument_fastapi_app(app)
    uninstrument_sqlalchemy_engine(engine)
    uninstrument_httpx()
    if _tracer_provider is not None:
        try:
            _tracer_provider.shutdown()
        except Exception:
            pass
    if _meter_provider is not None:
        try:
            _meter_provider.shutdown()
        except Exception:
            pass
    _initialized = False
    _tracer_provider = None
    _meter_provider = None
    _ml_instruments.clear()


# ==============================================================================
# Phase 4 — Application-Level ML Business Spans & Metrics Helpers
# ==============================================================================


def get_ml_tracer():
    """Returns the dedicated application-level ML Tracer."""
    return get_tracer("sentinelml.ml")


@contextmanager
def trace_ml_operation(operation_name: str, attributes: Optional[dict] = None):
    """
    Context manager to trace application-level ML business operations (e.g. model.inference, training.submit).
    Automatically captures exceptions, marks span error status, and preserves the active trace context hierarchy.
    Safe no-op when OTEL_ENABLED is False.
    """
    tracer = get_ml_tracer()
    with tracer.start_as_current_span(operation_name, attributes=attributes or {}) as span:
        try:
            yield span
        except Exception as exc:
            if span.is_recording():
                span.record_exception(exc)
                span.set_status(trace.StatusCode.ERROR, str(exc))
            raise


def get_ml_meter():
    """Returns the dedicated application-level ML Meter."""
    return get_meter("sentinelml.ml")


_ml_instruments: dict = {}


def _get_ml_counter(name: str, unit: str = "1", description: str = ""):
    global _ml_instruments
    if name not in _ml_instruments:
        meter = get_ml_meter()
        _ml_instruments[name] = meter.create_counter(name, unit=unit, description=description)
    return _ml_instruments[name]


def _get_ml_histogram(name: str, unit: str = "s", description: str = ""):
    global _ml_instruments
    if name not in _ml_instruments:
        meter = get_ml_meter()
        _ml_instruments[name] = meter.create_histogram(name, unit=unit, description=description)
    return _ml_instruments[name]


def record_training_job_submitted(framework: str = "custom", result: str = "success") -> None:
    """
    Increments 'training.jobs.submitted' counter.
    Uses only bounded, low-cardinality attributes: framework and result.
    """
    try:
        counter = _get_ml_counter(
            "training.jobs.submitted",
            unit="1",
            description="Total number of ML training jobs submitted.",
        )
        counter.add(1, {"framework": framework, "result": result})
    except Exception as exc:
        logger.debug(f"Notice: Failed to record training.jobs.submitted: {exc}")


def record_training_job_completed(status: str) -> None:
    """
    Increments 'training.jobs.completed' counter.
    Uses only bounded, low-cardinality status: SUCCEEDED / FAILED.
    """
    try:
        norm_status = status.upper() if status else "UNKNOWN"
        counter = _get_ml_counter(
            "training.jobs.completed",
            unit="1",
            description="Total number of ML training jobs completed.",
        )
        counter.add(1, {"status": norm_status})
    except Exception as exc:
        logger.debug(f"Notice: Failed to record training.jobs.completed: {exc}")


def record_training_duration(duration_seconds: float) -> None:
    """
    Records training duration in 'training.duration' histogram.
    """
    try:
        if duration_seconds is not None and duration_seconds >= 0:
            hist = _get_ml_histogram(
                "training.duration",
                unit="s",
                description="Training execution duration in seconds.",
            )
            hist.record(float(duration_seconds))
    except Exception as exc:
        logger.debug(f"Notice: Failed to record training.duration: {exc}")


def record_deployment_operation(operation: str, result: str) -> None:
    """
    Increments 'deployment.operations' counter.
    Uses only bounded, low-cardinality attributes: operation and result.
    """
    try:
        counter = _get_ml_counter(
            "deployment.operations",
            unit="1",
            description="Total model deployment operations executed.",
        )
        counter.add(1, {"operation": operation.lower(), "result": result.lower()})
    except Exception as exc:
        logger.debug(f"Notice: Failed to record deployment.operations: {exc}")


def record_inference_request(result: str = "success") -> None:
    """
    Increments 'inference.requests' counter.
    Uses only bounded, low-cardinality attribute: result.
    """
    try:
        counter = _get_ml_counter(
            "inference.requests",
            unit="1",
            description="Total model inference requests handled.",
        )
        counter.add(1, {"result": result.lower()})
    except Exception as exc:
        logger.debug(f"Notice: Failed to record inference.requests: {exc}")


def record_inference_duration(duration_seconds: float) -> None:
    """
    Records inference latency in 'inference.duration' histogram.
    """
    try:
        if duration_seconds is not None and duration_seconds >= 0:
            hist = _get_ml_histogram(
                "inference.duration",
                unit="s",
                description="Model inference end-to-end duration in seconds.",
            )
            hist.record(float(duration_seconds))
    except Exception as exc:
        logger.debug(f"Notice: Failed to record inference.duration: {exc}")



