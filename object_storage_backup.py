
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from collections.abc import Callable

try:
    import boto3
    from botocore.config import Config as BotoConfig
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:
    boto3 = None
    BotoConfig = None
    BotoCoreError = ClientError = Exception

DEFAULT_ATTEMPTS = 5
DEFAULT_DELAY_S = 3
_MD5_METADATA_KEY = "content-md5-hex"


class ObjectStorageError(Exception):
    pass


def is_configured() -> bool:

    return bool(
        os.environ.get("LINODE_OBJ_STORAGE_BUCKET")
        and os.environ.get("LINODE_OBJ_STORAGE_ENDPOINT")
        and os.environ.get("LINODE_OBJ_STORAGE_ACCESS_KEY")
        and os.environ.get("LINODE_OBJ_STORAGE_SECRET_KEY")
    )


_cached_client = None
_cached_bucket: str | None = None


def build_object_storage_client():

    global _cached_client, _cached_bucket
    if not is_configured() or boto3 is None:
        return None
    if _cached_client is not None:
        return _cached_client
    _cached_bucket = os.environ["LINODE_OBJ_STORAGE_BUCKET"]
    _cached_client = boto3.client(
        "s3",
        endpoint_url=os.environ["LINODE_OBJ_STORAGE_ENDPOINT"],
        aws_access_key_id=os.environ["LINODE_OBJ_STORAGE_ACCESS_KEY"],
        aws_secret_access_key=os.environ["LINODE_OBJ_STORAGE_SECRET_KEY"],
        config=BotoConfig(signature_version="s3v4"),
    )
    return _cached_client


def _object_key(name: str) -> str:
    return f"instances/{name}.json"


def _is_transient(e: Exception) -> bool:

    if isinstance(e, ClientError):
        status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        return status >= 500
    return isinstance(e, BotoCoreError)


def _retry(fn, *, attempts: int = DEFAULT_ATTEMPTS, delay_s: float = DEFAULT_DELAY_S):

    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except (ClientError, BotoCoreError) as e:
            last_exc = e
            if not _is_transient(e) or attempt == attempts - 1:
                raise
            time.sleep(delay_s)
    assert last_exc is not None
    raise last_exc


def _serialize(record: dict) -> tuple[bytes, str]:

    body = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    md5_hex = hashlib.md5(body).hexdigest()
    return body, md5_hex


def upload_instance_backup(
    name: str, record: dict, *, attempts: int = DEFAULT_ATTEMPTS, delay_s: float = DEFAULT_DELAY_S
) -> None:

    client = build_object_storage_client()
    if client is None:
        raise ObjectStorageError("Object Storage is not configured (LINODE_OBJ_STORAGE_* unset)")
    bucket = _cached_bucket
    body, md5_hex = _serialize(record)

    def _do():
        response = client.put_object(
            Bucket=bucket,
            Key=_object_key(name),
            Body=body,
            ContentType="application/json",
            Metadata={_MD5_METADATA_KEY: md5_hex},
        )
        etag = response.get("ETag", "").strip('"')
        if etag and etag != md5_hex:
            raise ObjectStorageError(
                f"Object Storage backup for '{name}' did not verify: expected ETag {md5_hex}, "
                f"got {etag} -- the write may not have persisted the exact bytes sent."
            )

    _retry(_do, attempts=attempts, delay_s=delay_s)


def snapshot_database(db_path: Path, dest_path: Path) -> None:

    source = sqlite3.connect(db_path)
    try:
        dest = sqlite3.connect(dest_path)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()


def upload_database_snapshot(
    db_path: Path, *, attempts: int = DEFAULT_ATTEMPTS, delay_s: float = DEFAULT_DELAY_S
) -> str:

    client = build_object_storage_client()
    if client is None:
        raise ObjectStorageError("Object Storage is not configured (LINODE_OBJ_STORAGE_* unset)")
    bucket = _cached_bucket

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir) / "snapshot.db"
        snapshot_database(db_path, tmp_path)
        body = tmp_path.read_bytes()

    md5_hex = hashlib.md5(body).hexdigest()
    key = f"full-backups/instances-{int(time.time())}.db"

    def _do():
        response = client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType="application/x-sqlite3",
            Metadata={_MD5_METADATA_KEY: md5_hex},
        )
        etag = response.get("ETag", "").strip('"')
        if etag and etag != md5_hex:
            raise ObjectStorageError(
                f"Full database snapshot upload to '{key}' did not verify: expected ETag "
                f"{md5_hex}, got {etag} -- the write may not have persisted the exact bytes sent."
            )

    _retry(_do, attempts=attempts, delay_s=delay_s)
    return key


def download_instance_backup(name: str) -> dict | None:

    client = build_object_storage_client()
    if client is None:
        return None

    def _do():
        return client.get_object(Bucket=_cached_bucket, Key=_object_key(name))

    try:
        response = _retry(_do)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        return None
    except (BotoCoreError, ObjectStorageError):
        return None

    body = response["Body"].read()
    stored_md5 = response.get("Metadata", {}).get(_MD5_METADATA_KEY)
    if stored_md5 and hashlib.md5(body).hexdigest() != stored_md5:
        return None
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def sync_object_storage_backup(
    name: str, record: dict, *, on_warning: Callable[[str], None] | None = None
) -> None:

    if not is_configured():
        return
    try:
        upload_instance_backup(name, record)
    except Exception as e:
        if on_warning is not None:
            on_warning(
                f"  WARNING: could not back up '{name}' to Object Storage ({e}) -- the local "
                "database write already succeeded and is fully in effect; this backup will "
                "self-heal on the next successful write for this instance (safe to retry: "
                "just run any command that touches it again)."
            )
