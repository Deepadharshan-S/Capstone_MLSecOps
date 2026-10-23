import hashlib
import threading
from typing import Any, Dict, Set, Tuple

import onnx

MAX_ONNX_FILE_SIZE_BYTES = 500 * 1024 * 1024  # 500 MB upload limit
MAX_ONNX_NODES = 50000
MAX_ONNX_INPUTS = 500
MAX_ONNX_OUTPUTS = 500
ALLOWED_OPERATOR_DOMAINS: Set[str] = {
    "",
    "ai.onnx",
    "ai.onnx.ml",
    "ai.onnx.preview.training",
}

# Bound concurrent CPU/memory-intensive ONNX validations to prevent DoS
_VALIDATION_SEMAPHORE = threading.BoundedSemaphore(3)


def _validate_graph_recursive(graph: Any, state: Dict[str, Any]) -> None:
    """
    Recursively inspects an ONNX graph and all nested subgraphs in control flow nodes:
    1. Blocks external tensor references across all initializers and attribute tensors.
    2. Validates operator domains across all nodes.
    3. Accumulates cumulative topology limits (nodes, inputs, outputs).
    """
    if not graph:
        return

    state["node_count"] += len(graph.node)
    state["input_count"] += len(graph.input)
    state["output_count"] += len(graph.output)

    if state["node_count"] > MAX_ONNX_NODES:
        raise ValueError(f"ONNX graph exceeds maximum allowed node limit ({state['node_count']} > {MAX_ONNX_NODES}).")
    if state["input_count"] > MAX_ONNX_INPUTS:
        raise ValueError(f"ONNX graph exceeds maximum allowed input tensor limit ({state['input_count']} > {MAX_ONNX_INPUTS}).")
    if state["output_count"] > MAX_ONNX_OUTPUTS:
        raise ValueError(f"ONNX graph exceeds maximum allowed output tensor limit ({state['output_count']} > {MAX_ONNX_OUTPUTS}).")

    # Check all initializers in this graph level
    initializers = list(graph.initializer) + list(getattr(graph, "sparse_initializer", []))
    for tensor in initializers:
        if hasattr(tensor, "data_location") and tensor.data_location == 1:
            raise ValueError("ONNX artifact rejected: Prohibited external tensor data reference.")
        if hasattr(tensor, "external_data") and len(tensor.external_data) > 0:
            raise ValueError("ONNX artifact rejected: Prohibited external tensor reference.")

    # Inspect all nodes and their attributes
    for node in graph.node:
        domain = node.domain or ""
        if domain not in ALLOWED_OPERATOR_DOMAINS:
            raise ValueError(f"ONNX model uses unsupported operator domain: '{domain}'")

        for attr in getattr(node, "attribute", []):
            if hasattr(attr, "t") and attr.t:
                if getattr(attr.t, "data_location", 0) == 1 or len(getattr(attr.t, "external_data", [])) > 0:
                    raise ValueError("ONNX artifact rejected: Prohibited external tensor in node attribute.")
            for t in getattr(attr, "tensors", []):
                if getattr(t, "data_location", 0) == 1 or len(getattr(t, "external_data", [])) > 0:
                    raise ValueError("ONNX artifact rejected: Prohibited external tensor in node attribute.")

            # Recursively inspect nested subgraphs in control-flow nodes (e.g. If, Loop, Scan)
            if hasattr(attr, "g") and attr.g and (len(attr.g.node) > 0 or len(attr.g.initializer) > 0):
                _validate_graph_recursive(attr.g, state)
            for sub_g in getattr(attr, "graphs", []):
                if sub_g and (len(sub_g.node) > 0 or len(sub_g.initializer) > 0):
                    _validate_graph_recursive(sub_g, state)


def validate_onnx_artifact_bytes(content_bytes: bytes) -> Tuple[Any, Dict[str, Any]]:
    """
    Strictly validates raw ONNX artifact bytes:
    1. Rejects zero-byte, oversized, or truncated inputs.
    2. Rejects pickle bytecodes disguised as ONNX.
    3. Verifies protobuf structure and checks model via onnx.checker.
    4. Blocks external tensor data references (TensorProto.EXTERNAL) recursively.
    5. Enforces graph-structure limits (node, input, output counts) across subgraphs.
    6. Restricts operator domains to safe, allowed domains.
    7. Computes and returns SHA-256 digest and metadata.
    """
    if not content_bytes:
        raise ValueError("ONNX artifact bytes are empty.")

    if len(content_bytes) > MAX_ONNX_FILE_SIZE_BYTES:
        raise ValueError(
            f"ONNX artifact size exceeds maximum allowed upload limit "
            f"({len(content_bytes)} bytes > {MAX_ONNX_FILE_SIZE_BYTES} bytes)."
        )

    # Reject pickle bytecodes (e.g. PROTO opcode 0x80 or GLOBAL 'c')
    if content_bytes[0] in (0x80, ord("c")):
        raise ValueError("File contains prohibited Python pickle bytecodes rather than valid ONNX protobuf data.")

    # Minimum size for a valid ONNX protobuf
    if len(content_bytes) < 16:
        raise ValueError("ONNX artifact is truncated or too small to be a valid model.")

    with _VALIDATION_SEMAPHORE:
        try:
            onnx_model = onnx.load_model_from_string(content_bytes)
        except Exception as err:
            raise ValueError(f"Malformed or invalid ONNX protobuf binary: {err}")

        if not hasattr(onnx_model, "graph") or onnx_model.graph is None:
            raise ValueError("ONNX artifact has no valid computation graph.")

        # Recursive check for external data, operator domains, and topology limits
        # Performed BEFORE onnx.checker to prevent local file inclusion attempts
        graph_state: Dict[str, Any] = {"node_count": 0, "input_count": 0, "output_count": 0}
        _validate_graph_recursive(onnx_model.graph, graph_state)

        try:
            onnx.checker.check_model(onnx_model)
        except Exception as check_err:
            raise ValueError(f"ONNX checker validation failed: {check_err}")

    sha256_hash = hashlib.sha256(content_bytes).hexdigest()

    metadata = {
        "sha256": sha256_hash,
        "byte_size": len(content_bytes),
        "ir_version": int(getattr(onnx_model, "ir_version", 8)),
        "producer_name": str(getattr(onnx_model, "producer_name", "unknown")),
        "node_count": graph_state["node_count"],
        "input_count": graph_state["input_count"],
        "output_count": graph_state["output_count"],
    }
    return onnx_model, metadata

