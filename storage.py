"""Storage abstraction para archivos binarios (driver documents).

Estrategia:
- Si AZURE_STORAGE_CONNECTION_STRING está set → Azure Blob Storage en el
  container DRIVER_DOCS_CONTAINER (default: 'driver-documents').
- Sino → filesystem local en DRIVER_DOCS_LOCAL_DIR (default: backend/uploads/).

API:
    storage.upload(blob_path, data, content_type) → None
    storage.download(blob_path) → (bytes, content_type)
    storage.delete(blob_path) → None

`blob_path` es una key relativa: 'drivers/DRV-001/uuid_nombre.pdf'.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Tuple

from loguru import logger


def _connection_string() -> Optional[str]:
    return os.environ.get("AZURE_STORAGE_CONNECTION_STRING") or None


def _container_name() -> str:
    return os.environ.get("DRIVER_DOCS_CONTAINER", "driver-documents")


def _local_dir() -> Path:
    base = os.environ.get("DRIVER_DOCS_LOCAL_DIR")
    if base:
        return Path(base)
    return Path(__file__).resolve().parent / "uploads"


def _is_azure_enabled() -> bool:
    return bool(_connection_string())


def _azure_client():
    """Devuelve un BlobServiceClient. Se importa lazy para no bloquear arranque."""
    from azure.storage.blob import BlobServiceClient
    return BlobServiceClient.from_connection_string(_connection_string())


def _ensure_container() -> None:
    if not _is_azure_enabled():
        return
    try:
        client = _azure_client()
        container = client.get_container_client(_container_name())
        if not container.exists():
            container.create_container()
            logger.info(f"[storage] container creado: {_container_name()}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[storage] no pude verificar/crear container: {e}")


def upload(blob_path: str, data: bytes, content_type: Optional[str] = None) -> None:
    """Sube `data` al path indicado. Si Azure no está configurado, escribe a fs."""
    if _is_azure_enabled():
        _ensure_container()
        from azure.storage.blob import ContentSettings
        client = _azure_client()
        blob = client.get_blob_client(container=_container_name(), blob=blob_path)
        blob.upload_blob(
            data,
            overwrite=True,
            content_settings=ContentSettings(content_type=content_type) if content_type else None,
        )
        return
    # Filesystem fallback
    target = _local_dir() / blob_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def download(blob_path: str) -> Tuple[bytes, Optional[str]]:
    """Descarga bytes + content_type. Lanza FileNotFoundError si no existe."""
    if _is_azure_enabled():
        client = _azure_client()
        blob = client.get_blob_client(container=_container_name(), blob=blob_path)
        if not blob.exists():
            raise FileNotFoundError(blob_path)
        stream = blob.download_blob()
        ct = None
        try:
            ct = blob.get_blob_properties().content_settings.content_type
        except Exception:  # noqa: BLE001
            pass
        return stream.readall(), ct
    target = _local_dir() / blob_path
    if not target.exists():
        raise FileNotFoundError(blob_path)
    return target.read_bytes(), None


def delete(blob_path: str) -> None:
    """Borra el blob/archivo. Idempotente (no falla si no existe)."""
    if _is_azure_enabled():
        try:
            client = _azure_client()
            blob = client.get_blob_client(container=_container_name(), blob=blob_path)
            blob.delete_blob()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[storage] delete falló para {blob_path}: {e}")
        return
    target = _local_dir() / blob_path
    if target.exists():
        target.unlink()


def storage_kind() -> str:
    """'azure' o 'filesystem'. Útil para reportar al cliente."""
    return "azure" if _is_azure_enabled() else "filesystem"
