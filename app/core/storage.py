"""Storage abstraction for binary files (documents).

Strategy:
- If AZURE_STORAGE_CONNECTION_STRING is set → Azure Blob Storage.
- Else → local filesystem at backend/uploads/.

API:
    upload(blob_path, data, content_type) → None
    download(blob_path) → (bytes, content_type | None)
    delete(blob_path) → None
"""
from __future__ import annotations

from pathlib import Path

from loguru import logger

from app.core.config import settings


def _connection_string() -> str | None:
    return settings.azure_storage_connection_string or None


def _container_name() -> str:
    return settings.azure_storage_container


def _local_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "uploads"


def _safe_local_path(blob_path: str) -> Path:
    """Resolve `blob_path` under the uploads dir, refusing any escape.

    Defense-in-depth against path traversal: callers validate `entity_id`, but
    this guarantees a crafted `blob_path` (e.g. '../../etc/passwd') can never
    read or write outside `_local_dir()`.
    """
    base = _local_dir().resolve()
    target = (base / blob_path).resolve()
    if target != base and base not in target.parents:
        raise ValueError(f"unsafe blob_path escapes storage root: {blob_path!r}")
    return target


def _is_azure() -> bool:
    return bool(_connection_string())


def _azure_client():
    from azure.storage.blob import BlobServiceClient
    return BlobServiceClient.from_connection_string(_connection_string())


def upload(blob_path: str, data: bytes, content_type: str | None = None) -> None:
    if _is_azure():
        from azure.storage.blob import ContentSettings
        client = _azure_client()
        blob = client.get_blob_client(container=_container_name(), blob=blob_path)
        blob.upload_blob(
            data,
            overwrite=True,
            content_settings=ContentSettings(content_type=content_type) if content_type else None,
        )
        logger.debug(f"[storage] uploaded {blob_path} to Azure ({len(data)} bytes)")
        return
    target = _safe_local_path(blob_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    logger.debug(f"[storage] saved {blob_path} to filesystem ({len(data)} bytes)")


def download(blob_path: str) -> tuple[bytes, str | None]:
    if _is_azure():
        client = _azure_client()
        blob = client.get_blob_client(container=_container_name(), blob=blob_path)
        if not blob.exists():
            raise FileNotFoundError(blob_path)
        stream = blob.download_blob()
        ct = None
        try:
            ct = blob.get_blob_properties().content_settings.content_type
        except Exception:
            pass
        return stream.readall(), ct
    target = _safe_local_path(blob_path)
    if not target.exists():
        raise FileNotFoundError(blob_path)
    return target.read_bytes(), None


def delete(blob_path: str) -> None:
    if _is_azure():
        try:
            client = _azure_client()
            blob = client.get_blob_client(container=_container_name(), blob=blob_path)
            blob.delete_blob()
        except Exception as e:
            logger.warning(f"[storage] delete failed for {blob_path}: {e}")
        return
    target = _safe_local_path(blob_path)
    if target.exists():
        target.unlink()


def storage_kind() -> str:
    return "azure" if _is_azure() else "filesystem"
