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
)
from fastapi.responses import StreamingResponse
import io
from sqlalchemy.orm import Session

from app.core.rate_limiter import RateLimiter
from app.api.permissions import get_current_active_user
from app.db.session import get_db
from app.models.user import User
from app.models.dataset import Dataset
from app.schemas import (
    DatasetResponse,
    DatasetCommitRequest,
    BranchCreateRequest,
    BranchResponse,
    TagCreateRequest,
    TagResponse,
    CommitResponse,
    CompareResponse,
    RollbackRequest,
    MetadataUpdateRequest,
)
from app.services.data_service import data_service

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
    user: User = Security(get_current_active_user),
):
    """
    Lists all registered datasets.
    """
    return data_service.list_datasets(db)


@router.get("/search", response_model=list[DatasetResponse])
def search_datasets(
    q: str,
    db: Session = Depends(get_db),
    user: User = Security(get_current_active_user),
):
    """
    Searches for datasets matching query string.
    """
    return data_service.search_datasets(db, query=q)


@router.get("/{dataset_name}", response_model=DatasetResponse)
def get_dataset(
    dataset_name: str,
    db: Session = Depends(get_db),
    user: User = Security(get_current_active_user),
):
    """
    Gets details of a single dataset.
    """
    dataset = db.query(Dataset).filter(Dataset.name == dataset_name).first()
    if not dataset:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Dataset not found"
        )
    return dataset


@router.delete("/{dataset_name}")
def delete_dataset(
    dataset_name: str,
    db: Session = Depends(get_db),
    user: User = Security(get_current_active_user, scopes=["datasets:upload"]),
):
    """
    Deletes a dataset (lakeFS repository and DB registration).
    """
    return data_service.delete_dataset(db, dataset_name, username=user.username)


@router.post(
    "/{dataset_name}/upload",
    dependencies=[Depends(RateLimiter(times=20, seconds=60))],
)
async def upload_file(
    dataset_name: str,
    branch: Optional[str] = "main",
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Security(get_current_active_user, scopes=["datasets:upload"]),
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
    user: User = Security(get_current_active_user),
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


@router.post("/{dataset_name}/branches", response_model=BranchResponse)
def create_branch(
    dataset_name: str,
    req: BranchCreateRequest,
    db: Session = Depends(get_db),
    user: User = Security(get_current_active_user, scopes=["datasets:upload"]),
):
    """
    Creates a new branch.
    """
    return data_service.create_branch(
        db=db,
        dataset_name=dataset_name,
        branch_name=req.name,
        source_branch=req.source_branch,
        username=user.username,
    )


@router.get("/{dataset_name}/branches", response_model=list[BranchResponse])
def list_branches(
    dataset_name: str,
    db: Session = Depends(get_db),
    user: User = Security(get_current_active_user),
):
    """
    Lists all branches in the repository.
    """
    return data_service.list_branches(db, dataset_name)


@router.delete("/{dataset_name}/branches/{branch_name}")
def delete_branch(
    dataset_name: str,
    branch_name: str,
    db: Session = Depends(get_db),
    user: User = Security(get_current_active_user, scopes=["datasets:upload"]),
):
    """
    Deletes a branch in the repository.
    """
    return data_service.delete_branch(
        db, dataset_name, branch_name, username=user.username
    )


@router.get("/{dataset_name}/commits", response_model=list[CommitResponse])
def view_commit_history(
    dataset_name: str,
    ref: Optional[str] = "main",
    limit: Optional[int] = None,
    db: Session = Depends(get_db),
    user: User = Security(get_current_active_user),
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
    db: Session = Depends(get_db),
    user: User = Security(get_current_active_user),
):
    """
    Compares two references (branches, commits, tags).
    """
    return data_service.compare_dataset_versions(db, dataset_name, left_ref, right_ref)


@router.post("/{dataset_name}/rollback")
def rollback_changes(
    dataset_name: str,
    req: RollbackRequest,
    db: Session = Depends(get_db),
    user: User = Security(get_current_active_user, scopes=["datasets:upload"]),
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


@router.post("/{dataset_name}/tags", response_model=TagResponse)
def create_tag(
    dataset_name: str,
    req: TagCreateRequest,
    db: Session = Depends(get_db),
    user: User = Security(get_current_active_user, scopes=["datasets:upload"]),
):
    """
    Creates a tag pointing to a reference.
    """
    return data_service.create_tag(
        db=db,
        dataset_name=dataset_name,
        tag_name=req.name,
        target_ref=req.target_ref,
        username=user.username,
    )


@router.get("/{dataset_name}/tags", response_model=list[TagResponse])
def list_tags(
    dataset_name: str,
    db: Session = Depends(get_db),
    user: User = Security(get_current_active_user),
):
    """
    Lists all tags in the repository.
    """
    return data_service.list_tags(db, dataset_name)


@router.delete("/{dataset_name}/tags/{tag_name}")
def delete_tag(
    dataset_name: str,
    tag_name: str,
    db: Session = Depends(get_db),
    user: User = Security(get_current_active_user, scopes=["datasets:upload"]),
):
    """
    Deletes a tag.
    """
    return data_service.delete_tag(db, dataset_name, tag_name, username=user.username)


@router.get("/{dataset_name}/metadata")
def get_dataset_metadata(
    dataset_name: str,
    db: Session = Depends(get_db),
    user: User = Security(get_current_active_user),
):
    """
    Retrieves metadata from both database and lakeFS.
    """
    return data_service.get_dataset_metadata(db, dataset_name)


@router.put("/{dataset_name}/metadata")
def update_dataset_metadata(
    dataset_name: str,
    req: MetadataUpdateRequest,
    db: Session = Depends(get_db),
    user: User = Security(get_current_active_user, scopes=["datasets:upload"]),
):
    """
    Updates the dataset metadata in Postgres database.
    """
    return data_service.update_dataset_metadata(
        db, dataset_name, req.metadata, username=user.username
    )
