from typing import Optional
from fastapi import (
    APIRouter,
    Security,
    status,
    Depends,
    HTTPException,
    UploadFile,
    File,
    Form,
    Query,
)
from fastapi.responses import StreamingResponse
import io
from sqlalchemy.orm import Session

from app.core.rate_limiter import RateLimiter
from app.api.permissions import get_current_active_user
from app.db.session import get_db
from app.models.user import User
from app.schemas import (
    DatasetResponse,
    DatasetCommitRequest,
    CommitResponse,
    CompareResponse,
    RollbackRequest,
    MetadataUpdateRequest,
    FileUploadResponse,
    RollbackResponse,
    DatasetMetadataResponse,
    DatasetMetadataUpdateResponse,
    MessageResponse,
)
from app.services.dependencies import get_data_service
from app.services.data_service import DataService

router = APIRouter(prefix="/datasets", tags=["datasets"])


@router.post(
    "",
    response_model=DatasetResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(RateLimiter(times=5, seconds=60))],
)
async def register_dataset(
    name: str = Form(...),
    description: Optional[str] = Form(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Security(get_current_active_user, scopes=["datasets:upload"]),
    data_service: DataService = Depends(get_data_service),
):
    """
    Registers a new dataset (creates database record and lakeFS repository) and uploads the dataset file.
    """
    db_dataset = data_service.register_dataset(
        db=db,
        dataset_name=name,
        description=description,
        user_id=user.id,
        username=user.username,
    )

    content = await file.read()
    file_path = file.filename
    if not file_path:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file must have a valid filename.",
        )

    data_service.upload_file(
        db=db,
        dataset_name=name,
        file_path=file_path,
        content=content,
        branch_name=db_dataset.default_branch,
        username=user.username,
    )

    return db_dataset


@router.get("", response_model=list[DatasetResponse])
def list_datasets(
    db: Session = Depends(get_db),
    user: User = Security(get_current_active_user, scopes=["datasets:upload"]),
    data_service: DataService = Depends(get_data_service),
):
    """
    Lists all registered datasets.
    """
    return data_service.list_datasets(db)


@router.delete("/{dataset_name}", response_model=MessageResponse)
def delete_dataset(
    dataset_name: str,
    db: Session = Depends(get_db),
    user: User = Security(get_current_active_user, scopes=["datasets:upload"]),
    data_service: DataService = Depends(get_data_service),
):
    """
    Deletes a dataset (lakeFS repository and DB registration).
    """
    return data_service.delete_dataset(db, dataset_name, username=user.username)


@router.post(
    "/{dataset_name}/upload",
    response_model=FileUploadResponse,
    dependencies=[Depends(RateLimiter(times=20, seconds=60))],
)
async def upload_file(
    dataset_name: str,
    branch: Optional[str] = "main",
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Security(get_current_active_user, scopes=["datasets:upload"]),
    data_service: DataService = Depends(get_data_service),
):
    """
    Uploads a file to the lakeFS repository.
    """
    content = await file.read()
    file_path = file.filename
    if not file_path:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file must have a valid filename.",
        )
    return data_service.upload_file(
        db=db,
        dataset_name=dataset_name,
        file_path=file_path,
        content=content,
        branch_name=branch,
        username=user.username,
    )


@router.get("/{dataset_name}/download")
def download_file(
    dataset_name: str,
    path: str,
    ref: Optional[str] = "main",
    db: Session = Depends(get_db),
    user: User = Security(get_current_active_user, scopes=["datasets:upload"]),
    data_service: DataService = Depends(get_data_service),
):
    """
    Downloads/retrieves a file stream from a reference (branch/commit/tag).
    """
    content = data_service.download_file(db, dataset_name, file_path=path, ref_id=ref)
    filename = path.split("/")[-1]
    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.post(
    "/{dataset_name}/commit",
    response_model=CommitResponse,
    dependencies=[Depends(RateLimiter(times=10, seconds=60))],
)
def commit_changes(
    dataset_name: str,
    req: DatasetCommitRequest,
    branch: Optional[str] = "main",
    db: Session = Depends(get_db),
    user: User = Security(get_current_active_user, scopes=["datasets:upload"]),
    data_service: DataService = Depends(get_data_service),
):
    """
    Commits changes on a branch.
    """
    return data_service.commit_changes(
        db=db,
        dataset_name=dataset_name,
        branch_name=branch,
        message=req.message,
        metadata=req.metadata,
        username=user.username,
    )


@router.get("/{dataset_name}/commits", response_model=list[CommitResponse])
def view_commit_history(
    dataset_name: str,
    ref: Optional[str] = "main",
    limit: Optional[int] = None,
    db: Session = Depends(get_db),
    user: User = Security(get_current_active_user, scopes=["datasets:upload"]),
    data_service: DataService = Depends(get_data_service),
):
    """
    Views commit history starting from a reference.
    """
    return data_service.view_commit_history(db, dataset_name, ref_id=ref, limit=limit)


@router.get("/{dataset_name}/compare", response_model=list[CompareResponse])
def compare_dataset_versions(
    dataset_name: str,
    left_ref: str,
    right_ref: str,
    type: str = Query("three_dot", pattern="^(three_dot|two_dot)$"),
    db: Session = Depends(get_db),
    user: User = Security(get_current_active_user, scopes=["datasets:upload"]),
    data_service: DataService = Depends(get_data_service),
):
    """
    Compares two references (branches, commits, tags).
    """
    return data_service.compare_dataset_versions(db, dataset_name, left_ref, right_ref, type)


@router.post("/{dataset_name}/rollback", response_model=RollbackResponse)
def rollback_changes(
    dataset_name: str,
    req: RollbackRequest,
    db: Session = Depends(get_db),
    user: User = Security(get_current_active_user, scopes=["datasets:upload"]),
    data_service: DataService = Depends(get_data_service),
):
    """
    Reverts a commit on a branch.
    """
    return data_service.rollback_changes(
        db=db,
        dataset_name=dataset_name,
        branch_name=req.branch,
        commit_id=req.commit_id,
        username=user.username,
    )


@router.get("/{dataset_name}", response_model=DatasetMetadataResponse)
def get_dataset_metadata(
    dataset_name: str,
    db: Session = Depends(get_db),
    user: User = Security(get_current_active_user, scopes=["datasets:upload"]),
    data_service: DataService = Depends(get_data_service),
):
    """
    Retrieves metadata from both database and lakeFS.
    """
    return data_service.get_dataset_metadata(db, dataset_name)


@router.put("/{dataset_name}", response_model=DatasetMetadataUpdateResponse)
def update_dataset_metadata(
    dataset_name: str,
    req: MetadataUpdateRequest,
    db: Session = Depends(get_db),
    user: User = Security(get_current_active_user, scopes=["datasets:upload"]),
    data_service: DataService = Depends(get_data_service),
):
    """
    Updates the dataset metadata in Postgres database.
    """
    return data_service.update_dataset_metadata(
        db, dataset_name, req.metadata, username=user.username
    )
