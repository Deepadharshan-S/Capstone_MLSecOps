from abc import ABC, abstractmethod
from typing import Optional, Any

class ObjectStorageService(ABC):
    @abstractmethod
    def delete_objects_with_prefix(self, bucket_name: str, prefix: str) -> None:
        """Deletes all objects in a bucket under the given prefix."""
        pass


class VersionControlService(ABC):
    @property
    @abstractmethod
    def client(self) -> Any:
        """Returns the low-level client or API driver instance."""
        pass

    @client.setter
    @abstractmethod
    def client(self, value: Any) -> None:
        """Sets the low-level client or API driver instance."""
        pass

    @abstractmethod
    def create_repository(self, repo_name: str, storage_ns: str, description: Optional[str] = None) -> None:
        """Creates a repository with an optional description."""
        pass

    @abstractmethod
    def delete_repository(self, repo_name: str) -> None:
        """Deletes a repository."""
        pass

    @abstractmethod
    def set_repository_metadata(self, repo_name: str, metadata: dict[str, str]) -> None:
        """Sets metadata for a repository."""
        pass

    @abstractmethod
    def get_repository_metadata(self, repo_name: str) -> dict[str, str]:
        """Gets metadata for a repository."""
        pass

    @abstractmethod
    def upload_file(self, repo_name: str, branch_name: str, file_path: str, content: bytes) -> None:
        """Uploads a file to a branch."""
        pass

    @abstractmethod
    def download_file(self, repo_name: str, ref_id: str, file_path: str) -> bytes:
        """Downloads a file at a specific ref."""
        pass

    @abstractmethod
    def commit(self, repo_name: str, branch_name: str, message: str, metadata: Optional[dict[str, str]]) -> dict:
        """Commits unstaged changes on a branch."""
        pass

    @abstractmethod
    def list_commits(self, repo_name: str, ref_id: str, limit: Optional[int] = None) -> list[dict]:
        """Lists commit history starting from a reference."""
        pass

    @abstractmethod
    def compare(self, repo_name: str, left_ref: str, right_ref: str, compare_type: str = "three_dot") -> list[dict]:
        """Compares two refs and returns a diff list."""
        pass

    @abstractmethod
    def rollback(self, repo_name: str, branch_name: str, commit_id: str) -> dict:
        """Reverts the changes of a commit on a branch."""
        pass

    @abstractmethod
    def create_branch(self, repo_name: str, branch_name: str, source_branch: str) -> str:
        """Creates a branch and returns the head commit ID."""
        pass

    @abstractmethod
    def list_branches(self, repo_name: str) -> list[dict]:
        """Lists all branches in a repository."""
        pass

    @abstractmethod
    def delete_branch(self, repo_name: str, branch_name: str) -> None:
        """Deletes a branch."""
        pass

    @abstractmethod
    def create_tag(self, repo_name: str, tag_name: str, target_ref: str) -> str:
        """Creates a tag pointing to a target ref."""
        pass

    @abstractmethod
    def list_tags(self, repo_name: str) -> list[dict]:
        """Lists all tags in a repository."""
        pass

    @abstractmethod
    def delete_tag(self, repo_name: str, tag_name: str) -> None:
        """Deletes a tag."""
        pass

    @abstractmethod
    def check_health(self) -> tuple[bool, str]:
        """Checks connection/health of the version control service backend. Returns (is_healthy, status_message)."""
        pass
