import ast
import hashlib
import io
import json
import logging
import os
import pickletools
import shutil
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.core.logging_config import log_audit_event

logger = logging.getLogger("ml_ops.pickle_scanner")

DANGEROUS_MODULES = {
    "os", "posix", "nt", "subprocess", "sys", "socket", "pty",
    "shutil", "urllib", "requests", "http", "builtins", "__builtin__",
    "ctypes", "importlib", "webbrowser", "commands"
}

DANGEROUS_CALLS = {
    "system", "popen", "popen2", "popen3", "popen4",
    "spawnl", "spawnle", "spawnlp", "spawnlpe",
    "spawnv", "spawnve", "spawnvp", "spawnvpe",
    "execl", "execle", "execlp", "execlpe",
    "execv", "execve", "execvp", "execvpe",
    "exec", "eval", "run", "call", "check_call", "check_output",
    "fork", "kill", "rmtree", "unlink", "remove", "socket", "connect"
}


class ModelPickleSecurityScanner:
    """
    Pickle Deserialization RCE Gatekeeper.
    Performs static AST disassembly and opcode inspection on .pkl/.joblib artifacts
    using Fickling and pickletools to detect embedded remote code execution payloads
    (os.system, subprocess, eval, exec) before deserialization.
    """

    def __init__(self, quarantine_dir: str = "logs/quarantine"):
        self.quarantine_dir = quarantine_dir

    def scan_bytes(self, data: bytes) -> Dict[str, Any]:
        """
        Inspects raw pickle bytecode without executing untrusted instructions.
        Combines two independent scanning engines:
          1. Bytecode opcode stream analysis via pickletools (tracks GLOBAL and STACK_GLOBAL).
          2. Static AST disassembly via Fickling.
        """
        violations: List[str] = []
        dangerous_globals: List[Tuple[str, str]] = []

        # Engine 1: Bytecode opcode inspection
        try:
            stack: List[Any] = []
            for opcode, arg, pos in pickletools.genops(data):
                if arg is not None:
                    stack.append(arg)

                # Legacy GLOBAL (protocol 0, 1, 2)
                if opcode.name == "GLOBAL":
                    if arg:
                        parts = arg.split(" ", 1)
                        mod = parts[0].strip() if len(parts) > 0 else ""
                        name = parts[1].strip() if len(parts) > 1 else ""
                        if mod in DANGEROUS_MODULES:
                            violations.append(f"Dangerous module import in opcode GLOBAL: {mod}.{name}")
                            dangerous_globals.append((mod, name))
                        elif name in DANGEROUS_CALLS:
                            violations.append(f"Dangerous callable in opcode GLOBAL: {mod}.{name}")
                            dangerous_globals.append((mod, name))

                # Modern STACK_GLOBAL (protocol 4+)
                elif opcode.name == "STACK_GLOBAL":
                    if len(stack) >= 2:
                        name = str(stack.pop()).strip()
                        mod = str(stack.pop()).strip()
                        if mod in DANGEROUS_MODULES:
                            violations.append(f"Dangerous module in STACK_GLOBAL opcode: {mod}.{name}")
                            dangerous_globals.append((mod, name))
                        elif name in DANGEROUS_CALLS:
                            violations.append(f"Dangerous callable in STACK_GLOBAL opcode: {mod}.{name}")
                            dangerous_globals.append((mod, name))

                # Clear stack periodically to bound memory on large models
                if len(stack) > 500:
                    stack = stack[-50:]
        except Exception as e:
            logger.debug(f"Opcode disassembly warning: {e}")

        # Engine 2: AST Disassembly via Fickling
        try:
            from fickling.fickle import Pickled

            pickled = Pickled.load(io.BytesIO(data))
            if pickled.ast:
                for node in ast.walk(pickled.ast):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            if alias.name in DANGEROUS_MODULES:
                                violations.append(f"Pickle AST imports dangerous module: {alias.name}")
                    elif isinstance(node, ast.ImportFrom):
                        if node.module and (
                            node.module in DANGEROUS_MODULES
                            or any(node.module.startswith(f"{m}.") for m in DANGEROUS_MODULES)
                        ):
                            violations.append(f"Pickle AST imports from dangerous module: {node.module}")
                    elif isinstance(node, ast.Call):
                        func_name = None
                        if isinstance(node.func, ast.Name):
                            func_name = node.func.id
                        elif isinstance(node.func, ast.Attribute):
                            func_name = node.func.attr

                        if func_name and func_name in DANGEROUS_CALLS:
                            violations.append(f"Pickle AST invokes dangerous function: {func_name}()")
        except Exception as e:
            logger.debug(f"Fickling AST disassembly warning: {e}")

        unique_violations = list(dict.fromkeys(violations))
        is_safe = len(unique_violations) == 0

        sha256 = hashlib.sha256(data).hexdigest()

        return {
            "is_safe": is_safe,
            "severity": "CRITICAL" if not is_safe else "NONE",
            "threat_type": "arbitrary_code_execution" if not is_safe else None,
            "violations": unique_violations,
            "dangerous_globals": dangerous_globals,
            "sha256": sha256,
            "size_bytes": len(data),
        }

    def scan_file(self, file_path: str) -> Dict[str, Any]:
        """Scans a file from disk and returns safety assessment."""
        with open(file_path, "rb") as f:
            data = f.read()
        return self.scan_bytes(data)

    def quarantine_file(
        self,
        source_path: str,
        filename: str,
        username: str,
        scan_result: Dict[str, Any],
        client_ip: Optional[str] = None,
    ) -> str:
        """
        Quarantines an unsafe model artifact into the secure quarantine vault.
        Writes metadata sidecar and logs a security audit event for threat hunting.
        """
        os.makedirs(self.quarantine_dir, exist_ok=True)
        sha256 = scan_result.get("sha256", "unknown")
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        safe_base = os.path.basename(filename)
        dest_filename = f"{timestamp}_{sha256[:12]}_{safe_base}"
        dest_path = os.path.join(self.quarantine_dir, dest_filename)

        # Move or copy the malicious file to quarantine
        shutil.copy2(source_path, dest_path)

        # Write quarantine metadata sidecar
        metadata = {
            "quarantined_at": datetime.now(timezone.utc).isoformat(),
            "original_filename": filename,
            "quarantine_path": dest_path,
            "uploaded_by": username,
            "client_ip": client_ip,
            "sha256": sha256,
            "size_bytes": scan_result.get("size_bytes", 0),
            "threat_type": scan_result.get("threat_type"),
            "severity": scan_result.get("severity"),
            "violations": scan_result.get("violations", []),
        }
        meta_path = f"{dest_path}.meta.json"
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2)

        # Log security audit event for automated Threat Hunting alert generation
        log_audit_event(
            action="model_rce_payload_detected",
            username=username,
            ip_address=client_ip,
            details=(
                f"Pickle Deserialization RCE Gatekeeper intercepted malicious model '{filename}'. "
                f"SHA256: {sha256}. Violations: {'; '.join(scan_result.get('violations', []))}. "
                f"File quarantined to '{dest_path}'."
            ),
        )

        logger.warning(
            f"SECURITY ALERT: Malicious model '{filename}' uploaded by '{username}' "
            f"quarantined to '{dest_path}'. Violations: {scan_result.get('violations')}"
        )
        return dest_path


pickle_scanner = ModelPickleSecurityScanner()
