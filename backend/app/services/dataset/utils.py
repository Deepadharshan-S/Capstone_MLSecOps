from __future__ import annotations
import re
from typing import Optional, Tuple
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.dataset import Dataset
from app.repositories.dataset_repository import DatasetRepository


def get_repo_name(name: str) -> str:
    """Sanitizes a dataset name to make it a valid lakeFS repository name."""
    sanitized = name.lower()
    # Replace any character that is not a lowercase letter, number, or dash with a dash
    sanitized = re.sub(r"[^a-z0-9-]", "-", sanitized)
    # Replace multiple consecutive dashes with a single dash
    sanitized = re.sub(r"-+", "-", sanitized)
    # Strip leading/trailing dashes
    sanitized = sanitized.strip("-")
    if len(sanitized) < 3:
        sanitized = (sanitized + "repo")[:3]
    if len(sanitized) > 63:
        sanitized = sanitized[:63].rstrip("-")
    return sanitized


def get_dataset_or_404(
    db: Session,
    dataset_name: str,
    repository: Optional[DatasetRepository] = None,
) -> Tuple[Dataset, str]:
    """
    Retrieves a Dataset from the database using DatasetRepository or raises an HTTP 404 error.
    Returns a tuple of (db_dataset, sanitized_repo_name).
    """
    repo = repository or DatasetRepository(db)
    dataset = repo.get_by_name(dataset_name)
    if not dataset:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Dataset '{dataset_name}' not found.",
        )
    return dataset, get_repo_name(dataset_name)
