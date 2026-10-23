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
import re
from urllib.parse import quote
from sqlalchemy.orm import Session

from app.core.rate_limiter import RateLimiter
from app.api.permissions import get_current_active_user
from app.db.session import get_db
from app.models.user import User
from app.schemas.auth import MessageResponse
from app.schemas.dataset import (
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
    CreateBranchRequest,
    BranchResponse,
    CreateTagRequest,
    TagResponse,
    FileEntryResponse,
    MergeBranchRequest,
    MergeBranchResponse,
)
from app.services.dependencies import get_data_service
from app.services.dataset import DataService

router = APIRouter(prefix="/datasets", tags=["datasets"])


@router.post(
    "",
    response_model=DatasetResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(RateLimiter(times=5, seconds=60))],
)
def register_dataset(
    name: str = Form(...),
    description: Optional[str] = Form(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Security(get_current_active_user, scopes=["datasets:upload"]),
    data_service: DataService = Depends(get_data_service),
):
    """
    Registers a new dataset (creates database record and lakeFS repository) and uploads the dataset file via streaming.
    """

    file_path = file.filename
    if not file_path or not file_path.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file must have a valid filename.",
        )

    import os
    clean_name = os.path.basename(file_path.strip())
    if not clean_name or ".." in file_path:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file has an invalid or unsafe filename.",
        )
    if file_path.startswith("_lakefs_actions") or "_lakefs_actions" in file_path:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Direct modification of system action configurations (_lakefs_actions) is forbidden.",
        )

    try:
        db_dataset = data_service.register_dataset(
            db=db,
            dataset_name=name,
            description=description,
            user_id=user.id,
            username=user.username,
        )
    except HTTPException as e:
        if e.status_code == 500 and "lakeFS" in str(e.detail):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    f"{e.detail} Is the lakeFS container running (docker compose up lakefs), "
                    "and are LAKEFS_ENDPOINT / LAKEFS_ACCESS_KEY_ID / LAKEFS_SECRET_ACCESS_KEY set?"
                ),
            )
        raise

    try:
        # Safe Bootstrap: 1. Install pre-merge Action configuration on main
        import os
        from app.core.config import settings

        action_template_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "templates",
            "poisoning_gate_action.yaml",
        )
        if not os.path.exists(action_template_path):
            raise RuntimeError(f"Action template not found at {action_template_path}")

        with open(action_template_path, "r", encoding="utf-8") as af:
            action_yaml = af.read()

        webhook_url = settings.LAKEFS_WEBHOOK_URL
        webhook_secret = settings.LAKEFS_WEBHOOK_SECRET
        if getattr(settings, "POISONING_ALLOW_QUERY_PARAM_AUTH", False):
            target_url = f"{webhook_url}?secret={webhook_secret}" if "?" not in webhook_url else f"{webhook_url}&secret={webhook_secret}"
        else:
            target_url = webhook_url
        target_default_branch = db_dataset.default_branch or getattr(settings, "LAKEFS_DEFAULT_BRANCH", "main")
        action_yaml = (
            action_yaml
            .replace("{{ default_branch }}", target_default_branch)
            .replace("{{ webhook_url }}", target_url)
            .replace("{{ webhook_secret }}", webhook_secret)
        )

        data_service.upload_file(
            db=db,
            dataset_name=name,
            file_path="_lakefs_actions/poisoning_gate.yaml",
            content=action_yaml.encode("utf-8"),
            branch_name=db_dataset.default_branch,
            username=user.username,
        )
        data_service.commit_changes(
            db=db,
            dataset_name=name,
            branch_name=db_dataset.default_branch,
            message="Install statistical data poisoning gate action",
            metadata=None,
            username=user.username,
        )

        # Safe Bootstrap: 2. Upload and commit initial trusted baseline on main
        data_service.upload_file(
            db=db,
            dataset_name=name,
            file_path=file_path,
            content=file.file,
            branch_name=db_dataset.default_branch,
            username=user.username,
        )
        data_service.commit_changes(
            db=db,
            dataset_name=name,
            branch_name=db_dataset.default_branch,
            message=f"Initial trusted baseline: {file_path}",
            metadata=None,
            username=user.username,
        )

        # Safe Bootstrap: 3. Arm branch protection on main
        data_service.set_branch_protection(
            db=db,
            dataset_name=name,
            pattern=db_dataset.default_branch,
            username=user.username,
        )

    except Exception as bootstrap_err:
        import logging
        logging.getLogger("datasets").error(f"Security bootstrap failed for dataset '{name}': {bootstrap_err}")
        try:
            data_service.delete_dataset(db, name, username=user.username)
        except Exception as cleanup_err:
            logging.getLogger("datasets").error(f"Failed to cleanup dataset '{name}' after bootstrap failure: {cleanup_err}")
        if isinstance(bootstrap_err, HTTPException):
            raise bootstrap_err
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Dataset registration aborted: security bootstrap failed ({str(bootstrap_err)})",
        )

    return db_dataset


@router.get("", response_model=list[DatasetResponse])
def list_datasets(
    limit: int = Query(default=100, ge=1, le=1000, description="Max number of datasets to retrieve"),
    offset: int = Query(default=0, ge=0, description="Number of datasets to skip"),
    db: Session = Depends(get_db),
    user: User = Security(get_current_active_user, scopes=["datasets:view"]),
    data_service: DataService = Depends(get_data_service),
):
    """
    Lists registered datasets with database-level pagination.
    """
    return data_service.list_datasets(db, limit=limit, offset=offset)


@router.delete("/{dataset_name}", response_model=MessageResponse)
def delete_dataset(
    dataset_name: str,
    db: Session = Depends(get_db),
    user: User = Security(get_current_active_user, scopes=["datasets:delete"]),
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
def upload_file(
    dataset_name: str,
    branch: Optional[str] = Query(None),
    branch_form: Optional[str] = Form(None, alias="branch"),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Security(get_current_active_user, scopes=["datasets:upload"]),
    data_service: DataService = Depends(get_data_service),
):
    """
    Uploads a file to the lakeFS repository via streaming.
    If the target branch is protected against direct writes (e.g. 'main'),
    rejects direct writes with HTTP 400 so changes must be pushed to a feature
    branch and validated before merging.
    """
    file_path = file.filename
    if not file_path or not file_path.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file must have a valid filename.",
        )

    import os
    clean_name = os.path.basename(file_path.strip())
    if not clean_name or ".." in file_path:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file has an invalid or unsafe filename.",
        )
    if file_path.startswith("_lakefs_actions") or "_lakefs_actions" in file_path:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Direct modification of system action configurations (_lakefs_actions) is forbidden.",
        )

    target_branch = branch_form or branch or "main"
    protected_rules = data_service.get_branch_protection(db, dataset_name=dataset_name)
    if target_branch in protected_rules:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Branch '{target_branch}' is protected against direct writes. "
                f"Please create or select a feature branch to upload files, then merge into '{target_branch}'."
            ),
        )

    return data_service.upload_file(
        db=db,
        dataset_name=dataset_name,
        file_path=file_path,
        content=file.file,
        branch_name=target_branch,
        username=user.username,
    )


@router.get("/{dataset_name}/download")
def download_file(
    dataset_name: str,
    path: str,
    ref: Optional[str] = "main",
    db: Session = Depends(get_db),
    user: User = Security(get_current_active_user, scopes=["datasets:view"]),
    data_service: DataService = Depends(get_data_service),
):
    """
    Downloads/retrieves a chunked file stream from a reference (branch/commit/tag).
    """
    raw_filename = path.split("/")[-1] or "downloaded_file"
    filename = re.sub(r'[^a-zA-Z0-9_.-]', '_', raw_filename)
    encoded_filename = quote(filename)
    chunk_stream = data_service.stream_file(db, dataset_name, file_path=path, ref_id=ref)
    return StreamingResponse(
        chunk_stream,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename=\"{filename}\"; filename*=UTF-8''{encoded_filename}"
        },
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
    If the target branch is protected against direct writes (e.g. 'main'),
    rejects direct commits with HTTP 400 so changes must be committed to a feature
    branch and validated via merge.
    """
    target_branch = branch or "main"
    protected_rules = data_service.get_branch_protection(db, dataset_name=dataset_name)
    if target_branch in protected_rules:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Branch '{target_branch}' is protected against direct writes. "
                f"Please commit changes on a feature branch, then merge into '{target_branch}'."
            ),
        )

    return data_service.commit_changes(
        db=db,
        dataset_name=dataset_name,
        branch_name=target_branch,
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
    user: User = Security(get_current_active_user, scopes=["datasets:view"]),
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
    user: User = Security(get_current_active_user, scopes=["datasets:view"]),
    data_service: DataService = Depends(get_data_service),
):
    """
    Compares two references (branches, commits, tags).
    """
    return data_service.compare_dataset_versions(db, dataset_name, left_ref, right_ref, type)


@router.post(
    "/{dataset_name}/rollback",
    response_model=RollbackResponse,
    dependencies=[Depends(RateLimiter(times=10, seconds=60))],
)
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
    user: User = Security(get_current_active_user, scopes=["datasets:view"]),
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


# --- Branch Management Endpoints ---


@router.get("/{dataset_name}/files", response_model=list[FileEntryResponse])
def list_files(
    dataset_name: str,
    ref: Optional[str] = "main",
    db: Session = Depends(get_db),
    user: User = Security(get_current_active_user, scopes=["datasets:view"]),
    data_service: DataService = Depends(get_data_service),
):
    """
    Lists objects stored under a ref (branch, tag or commit id).
    Powers the Files-tab preview picker — no path typing required.
    """
    return data_service.list_files(db, dataset_name, ref_id=ref or "main")


@router.get("/{dataset_name}/branches", response_model=list[BranchResponse])
def list_branches(
    dataset_name: str,
    db: Session = Depends(get_db),
    user: User = Security(get_current_active_user, scopes=["datasets:view"]),
    data_service: DataService = Depends(get_data_service),
):
    """
    Lists all branches in the dataset's lakeFS repository.
    """
    return data_service.list_branches(db, dataset_name)


@router.post(
    "/{dataset_name}/branches",
    response_model=BranchResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(RateLimiter(times=10, seconds=60))],
)
def create_branch(
    dataset_name: str,
    req: CreateBranchRequest,
    db: Session = Depends(get_db),
    user: User = Security(get_current_active_user, scopes=["datasets:upload"]),
    data_service: DataService = Depends(get_data_service),
):
    """
    Creates a new branch from a source branch in the dataset repository.
    """
    return data_service.create_branch(
        db=db,
        dataset_name=dataset_name,
        branch_name=req.branch_name,
        source_branch=req.source_branch or "main",
        username=user.username,
    )


@router.delete("/{dataset_name}/branches/{branch_name}", response_model=MessageResponse)
def delete_branch(
    dataset_name: str,
    branch_name: str,
    db: Session = Depends(get_db),
    user: User = Security(get_current_active_user, scopes=["datasets:delete"]),
    data_service: DataService = Depends(get_data_service),
):
    """
    Deletes a branch in the dataset repository (default branch cannot be deleted).
    """
    return data_service.delete_branch(
        db=db,
        dataset_name=dataset_name,
        branch_name=branch_name,
        username=user.username,
    )


@router.post(
    "/{dataset_name}/merge",
    response_model=MergeBranchResponse,
    dependencies=[Depends(RateLimiter(times=10, seconds=60))],
)
def merge_branch(
    dataset_name: str,
    req: MergeBranchRequest,
    db: Session = Depends(get_db),
    user: User = Security(get_current_active_user, scopes=["datasets:upload"]),
    data_service: DataService = Depends(get_data_service),
):
    """
    Merges a candidate staging branch into the protected baseline branch ('main').
    Synchronously triggers pre-merge statistical validation actions (Kolmogorov-Smirnov,
    target-label Chi-Square homogeneity, and PSI checks).
    Rejects merges exhibiting anomalous distribution shifts with HTTP 422.
    """
    return data_service.merge_branches(
        db=db,
        dataset_name=dataset_name,
        source_branch=req.source_branch,
        destination_branch=req.destination_branch or "main",
        message=req.message,
        username=user.username,
    )


# --- Tag Management Endpoints ---


@router.get("/{dataset_name}/tags", response_model=list[TagResponse])
def list_tags(
    dataset_name: str,
    db: Session = Depends(get_db),
    user: User = Security(get_current_active_user, scopes=["datasets:view"]),
    data_service: DataService = Depends(get_data_service),
):
    """
    Lists all tags in the dataset repository.
    """
    return data_service.list_tags(db, dataset_name)


@router.post(
    "/{dataset_name}/tags",
    response_model=TagResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(RateLimiter(times=10, seconds=60))],
)
def create_tag(
    dataset_name: str,
    req: CreateTagRequest,
    db: Session = Depends(get_db),
    user: User = Security(get_current_active_user, scopes=["datasets:upload"]),
    data_service: DataService = Depends(get_data_service),
):
    """
    Creates a tag pointing to a specific commit or branch reference in the dataset repository.
    """
    return data_service.create_tag(
        db=db,
        dataset_name=dataset_name,
        tag_name=req.tag_name,
        target_ref=req.target_ref or "main",
        username=user.username,
    )


@router.delete("/{dataset_name}/tags/{tag_name}", response_model=MessageResponse)
def delete_tag(
    dataset_name: str,
    tag_name: str,
    db: Session = Depends(get_db),
    user: User = Security(get_current_active_user, scopes=["datasets:delete"]),
    data_service: DataService = Depends(get_data_service),
):
    """
    Deletes a tag in the dataset repository.
    """
    return data_service.delete_tag(
        db=db,
        dataset_name=dataset_name,
        tag_name=tag_name,
        username=user.username,
    )
