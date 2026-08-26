import lakefs
import lakefs_sdk
from typing import Optional, Any
from app.core.config import settings

class LakeFSService:
    """
    Dedicated service handling low-level and high-level direct client interactions with the lakeFS API and SDK.
    """
    def __init__(self):
        try:
            self.client = lakefs.Client(
                username=settings.LAKEFS_ACCESS_KEY_ID,
                password=settings.LAKEFS_SECRET_ACCESS_KEY,
                host=settings.LAKEFS_ENDPOINT,
            )
        except Exception:
            self.client = None

    def create_repository(self, repo_name: str, storage_ns: str, description: Optional[str] = None) -> None:
        if not self.client:
            raise RuntimeError("lakeFS client is not initialized.")
        repo = lakefs.Repository(repo_name, client=self.client)
        repo.create(
            storage_namespace=storage_ns,
            default_branch=settings.LAKEFS_DEFAULT_BRANCH,
            exist_ok=True,
        )
        if description:
            self.set_repository_metadata(repo_name, {"description": description})

    def delete_repository(self, repo_name: str) -> None:
        if not self.client:
            raise RuntimeError("lakeFS client is not initialized.")
        repo = lakefs.Repository(repo_name, client=self.client)
        repo.delete()

    def set_repository_metadata(self, repo_name: str, metadata: dict[str, str]) -> None:
        if not self.client:
            raise RuntimeError("lakeFS client is not initialized.")
        self.client.sdk_client.internal_api.set_repository_metadata(
            repository=repo_name,
            repository_metadata_set=lakefs_sdk.RepositoryMetadataSet(
                metadata=metadata
            ),
        )

    def get_repository_metadata(self, repo_name: str) -> dict[str, str]:
        if not self.client:
            return {}
        try:
            repo = lakefs.Repository(repo_name, client=self.client)
            return repo.metadata or {}
        except Exception:
            return {}

    def upload_file(self, repo_name: str, branch_name: str, file_path: str, content: bytes) -> None:
        if not self.client:
            raise RuntimeError("lakeFS client is not initialized.")
        repo = lakefs.Repository(repo_name, client=self.client)
        branch = repo.branch(branch_name)
        obj = branch.object(file_path)
        obj.upload(content, mode="wb")

    def download_file(self, repo_name: str, ref_id: str, file_path: str) -> bytes:
        if not self.client:
            raise RuntimeError("lakeFS client is not initialized.")
        repo = lakefs.Repository(repo_name, client=self.client)
        ref = repo.ref(ref_id)
        obj = ref.object(file_path)
        with obj.reader(mode="rb") as reader:
            return reader.read()

    def commit(self, repo_name: str, branch_name: str, message: str, metadata: Optional[dict[str, str]]) -> dict:
        if not self.client:
            raise RuntimeError("lakeFS client is not initialized.")
        repo = lakefs.Repository(repo_name, client=self.client)
        branch = repo.branch(branch_name)
        ref = branch.commit(message=message, metadata=metadata)
        commit_details = ref.get_commit()
        return {
            "id": ref.id,
            "parents": [p for p in getattr(commit_details, "parents", [])],
            "committer": getattr(commit_details, "committer", "unknown"),
            "message": message,
            "creation_date": getattr(commit_details, "creation_date", 0),
            "metadata": metadata or {},
        }

    def list_commits(self, repo_name: str, ref_id: str, limit: Optional[int] = None) -> list[dict]:
        if not self.client:
            raise RuntimeError("lakeFS client is not initialized.")
        repo = lakefs.Repository(repo_name, client=self.client)
        ref = repo.ref(ref_id)
        log_gen = ref.log(max_amount=limit) if limit else ref.log()
        commits = []
        for c in log_gen:
            commits.append(
                {
                    "id": c.id,
                    "parents": list(c.parents or []),
                    "committer": c.committer,
                    "message": c.message,
                    "creation_date": c.creation_date,
                    "metadata": c.metadata or {},
                }
            )
        return commits

    def compare(self, repo_name: str, left_ref: str, right_ref: str, compare_type: str = "three_dot") -> list[dict]:
        if not self.client:
            raise RuntimeError("lakeFS client is not initialized.")
        repo = lakefs.Repository(repo_name, client=self.client)
        ref = repo.ref(left_ref)
        changes = []
        for change in ref.diff(other_ref=right_ref, type=compare_type):
            changes.append(
                {
                    "type": change.type,
                    "path": change.path,
                    "path_type": change.path_type,
                    "size_bytes": change.size_bytes,
                }
            )
        return changes

    def rollback(self, repo_name: str, branch_name: str, commit_id: str) -> dict:
        if not self.client:
            raise RuntimeError("lakeFS client is not initialized.")
        repo = lakefs.Repository(repo_name, client=self.client)
        branch = repo.branch(branch_name)
        reverted_commit = branch.revert(reference=commit_id)
        return {
            "new_commit_id": reverted_commit.id,
            "reverted_commit_id": commit_id,
        }

    def create_branch(self, repo_name: str, branch_name: str, source_branch: str) -> str:
        if not self.client:
            raise RuntimeError("lakeFS client is not initialized.")
        repo = lakefs.Repository(repo_name, client=self.client)
        branch = repo.branch(branch_name)
        branch.create(source_reference=source_branch, exist_ok=False)
        return branch.head.id

    def list_branches(self, repo_name: str) -> list[dict]:
        if not self.client:
            raise RuntimeError("lakeFS client is not initialized.")
        repo = lakefs.Repository(repo_name, client=self.client)
        branches = []
        for b in repo.branches():
            try:
                head_id = b.head.id
            except Exception:
                head_id = "unknown"
            branches.append({"name": b.id, "head_commit_id": head_id})
        return branches

    def delete_branch(self, repo_name: str, branch_name: str) -> None:
        if not self.client:
            raise RuntimeError("lakeFS client is not initialized.")
        repo = lakefs.Repository(repo_name, client=self.client)
        branch = repo.branch(branch_name)
        branch.delete()

    def create_tag(self, repo_name: str, tag_name: str, target_ref: str) -> str:
        if not self.client:
            raise RuntimeError("lakeFS client is not initialized.")
        repo = lakefs.Repository(repo_name, client=self.client)
        tag = repo.tag(tag_name)
        tag.create(source_ref=target_ref, exist_ok=False)
        try:
            return tag.get_commit().id
        except Exception:
            return target_ref

    def list_tags(self, repo_name: str) -> list[dict]:
        if not self.client:
            raise RuntimeError("lakeFS client is not initialized.")
        repo = lakefs.Repository(repo_name, client=self.client)
        tags = []
        for t in repo.tags():
            try:
                commit_id = t.get_commit().id
            except Exception:
                commit_id = "unknown"
            tags.append({"name": t.id, "commit_id": commit_id})
        return tags

    def delete_tag(self, repo_name: str, tag_name: str) -> None:
        if not self.client:
            raise RuntimeError("lakeFS client is not initialized.")
        repo = lakefs.Repository(repo_name, client=self.client)
        tag = repo.tag(tag_name)
        tag.delete()

lakefs_service = LakeFSService()
