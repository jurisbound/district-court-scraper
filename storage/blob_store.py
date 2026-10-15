"""One place that opens the PDF storage, for every scraper and pipeline step.

The scrapers were written for S3-style storage (boto3): Supabase Storage today.
Azure Blob Storage does not speak S3, so this module offers the same seven
calls the scrapers use, done with the Azure library. The scrapers keep calling
`put_object`, `get_object`, `head_object`, `upload_file`, `download_file`,
`delete_object` and `get_paginator("list_objects_v2")` exactly as before.

Which storage is used is one setting:

    STORAGE_BACKEND=s3       Supabase Storage (S3 API) — the default, unchanged
    STORAGE_BACKEND=azure    Azure Blob Storage

    s3:     SUPABASE_S3_ENDPOINT, SUPABASE_S3_REGION, SUPABASE_S3_ACCESS_KEY, SUPABASE_S3_SECRET_KEY
    azure:  AZURE_STORAGE_CONNECTION_STRING

The bucket name the scrapers pass (SUPABASE_S3_BUCKET, "court-documents") is the
Azure container name, so paths stay the same on both.

    from storage.blob_store import storage_client, public_url
    s3 = storage_client()
    s3.put_object(Bucket="court-documents", Key="a/b.pdf", Body=data, ContentType="application/pdf")
"""
from __future__ import annotations

import io
import os
from typing import Any, Iterator, Optional

BACKEND_ENV = "STORAGE_BACKEND"


def backend() -> str:
    return (os.environ.get(BACKEND_ENV) or "s3").strip().lower()


def storage_client(**s3_kwargs: Any):
    """The storage client for the configured backend.

    s3:    a boto3 client for Supabase Storage (same settings as before); extra
           keyword arguments (for example a botocore Config) are passed to boto3.
    azure: an AzureBlobAsS3 object with the same methods.
    """
    if backend() == "azure":
        return AzureBlobAsS3(os.environ["AZURE_STORAGE_CONNECTION_STRING"])
    import boto3
    from botocore.config import Config

    s3_kwargs.setdefault("config", Config(s3={"addressing_style": "path"}, signature_version="s3v4"))
    return boto3.client(
        "s3",
        endpoint_url=os.environ["SUPABASE_S3_ENDPOINT"],
        region_name=os.environ.get("SUPABASE_S3_REGION", "ap-south-1"),
        aws_access_key_id=os.environ["SUPABASE_S3_ACCESS_KEY"],
        aws_secret_access_key=os.environ["SUPABASE_S3_SECRET_KEY"],
        **s3_kwargs,
    )


def public_url(key: str, bucket: Optional[str] = None) -> Optional[str]:
    """Address anyone can open for a stored file (the bucket / container is public-read)."""
    bucket = bucket or os.environ.get("SUPABASE_S3_BUCKET", "court-documents")
    key = key.lstrip("/")
    if backend() == "azure":
        account = _account_name(os.environ.get("AZURE_STORAGE_CONNECTION_STRING", ""))
        return f"https://{account}.blob.core.windows.net/{bucket}/{key}" if account else None
    base = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
    return f"{base}/storage/v1/object/public/{bucket}/{key}" if base else None


def _account_name(connection_string: str) -> Optional[str]:
    for part in connection_string.split(";"):
        if part.startswith("AccountName="):
            return part.split("=", 1)[1]
    return None


# --------------------------------------------------------------------------- errors
# The scrapers recognise a missing file by botocore's ClientError with the code
# "NoSuchKey" (get) or "404" (head), or by `client.exceptions.NoSuchKey`.
# The Azure side raises the same kind of error, so that code works unchanged.

def _client_error(code: str, operation: str, key: str):
    from botocore.exceptions import ClientError

    cls = NoSuchKey if code == "NoSuchKey" else ClientError
    return cls({"Error": {"Code": code, "Message": f"{key} not found"},
                "ResponseMetadata": {"HTTPStatusCode": 404}}, operation)


def _no_such_key_class():
    from botocore.exceptions import ClientError

    class NoSuchKey(ClientError):  # noqa: D401 - mirrors client.exceptions.NoSuchKey
        pass

    return NoSuchKey


NoSuchKey = _no_such_key_class()


class _Exceptions:
    NoSuchKey = NoSuchKey

    @property
    def ClientError(self):  # noqa: N802 - boto3 attribute name
        from botocore.exceptions import ClientError

        return ClientError


class _Body:
    """Stands in for boto3's StreamingBody: .read(), .iter_chunks(), .close()."""

    def __init__(self, data: bytes):
        self._io = io.BytesIO(data)

    def read(self, amt: Optional[int] = None) -> bytes:
        return self._io.read() if amt is None else self._io.read(amt)

    def iter_chunks(self, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
        while True:
            chunk = self._io.read(chunk_size)
            if not chunk:
                return
            yield chunk

    def close(self) -> None:
        self._io.close()


class _ListPaginator:
    """get_paginator("list_objects_v2").paginate(Bucket=, Prefix=) -> pages like boto3's."""

    PAGE = 1000

    def __init__(self, store: "AzureBlobAsS3"):
        self._store = store

    def paginate(self, Bucket: str, Prefix: str = "", **_ignored) -> Iterator[dict]:  # noqa: N803
        container = self._store._container(Bucket)
        page: list[dict] = []
        for blob in container.list_blobs(name_starts_with=Prefix or None):
            page.append({"Key": blob.name, "Size": blob.size, "LastModified": blob.last_modified,
                         "ETag": blob.etag})
            if len(page) == self.PAGE:
                yield {"Contents": page, "KeyCount": len(page), "IsTruncated": True}
                page = []
        # boto3 leaves "Contents" out of a page with no objects
        yield ({"Contents": page, "KeyCount": len(page), "IsTruncated": False} if page
               else {"KeyCount": 0, "IsTruncated": False})


class AzureBlobAsS3:
    """The S3 calls the scrapers use, done on Azure Blob Storage."""

    def __init__(self, connection_string: str):
        from azure.storage.blob import BlobServiceClient

        self._service = BlobServiceClient.from_connection_string(connection_string)
        self.exceptions = _Exceptions()

    # -- helpers
    def _container(self, bucket: str):
        return self._service.get_container_client(bucket)

    def _blob(self, bucket: str, key: str):
        return self._service.get_blob_client(container=bucket, blob=key)

    @staticmethod
    def _content_settings(content_type: Optional[str]):
        from azure.storage.blob import ContentSettings

        return ContentSettings(content_type=content_type) if content_type else None

    # -- the seven calls
    def put_object(self, Bucket: str, Key: str, Body: Any = b"", ContentType: Optional[str] = None,  # noqa: N803
                   **_ignored) -> dict:
        data = Body.encode("utf-8") if isinstance(Body, str) else Body
        if hasattr(data, "read"):
            data = data.read()
        props = self._blob(Bucket, Key).upload_blob(data, overwrite=True,
                                                     content_settings=self._content_settings(ContentType))
        return {"ETag": props.get("etag"), "ResponseMetadata": {"HTTPStatusCode": 200}}

    def get_object(self, Bucket: str, Key: str, **_ignored) -> dict:  # noqa: N803
        from azure.core.exceptions import ResourceNotFoundError

        try:
            downloader = self._blob(Bucket, Key).download_blob()
        except ResourceNotFoundError:
            raise _client_error("NoSuchKey", "GetObject", Key) from None
        data = downloader.readall()
        props = downloader.properties
        return {"Body": _Body(data), "ContentLength": len(data), "LastModified": props.last_modified,
                "ContentType": props.content_settings.content_type, "ETag": props.etag,
                "ResponseMetadata": {"HTTPStatusCode": 200}}

    def head_object(self, Bucket: str, Key: str, **_ignored) -> dict:  # noqa: N803
        from azure.core.exceptions import ResourceNotFoundError

        try:
            props = self._blob(Bucket, Key).get_blob_properties()
        except ResourceNotFoundError:
            raise _client_error("404", "HeadObject", Key) from None
        return {"ContentLength": props.size, "LastModified": props.last_modified,
                "ContentType": props.content_settings.content_type, "ETag": props.etag,
                "ResponseMetadata": {"HTTPStatusCode": 200}}

    def upload_file(self, Filename: str, Bucket: str, Key: str, ExtraArgs: Optional[dict] = None,  # noqa: N803
                    Config: Any = None, **_ignored) -> None:
        content_type = (ExtraArgs or {}).get("ContentType")
        with open(Filename, "rb") as f:
            # large files are sent in blocks by the Azure library itself
            self._blob(Bucket, Key).upload_blob(f, overwrite=True, max_concurrency=4,
                                                content_settings=self._content_settings(content_type))

    def download_file(self, Bucket: str, Key: str, Filename: str, ExtraArgs: Any = None,  # noqa: N803
                      Config: Any = None, **_ignored) -> None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            downloader = self._blob(Bucket, Key).download_blob(max_concurrency=4)
        except ResourceNotFoundError:
            raise _client_error("404", "HeadObject", Key) from None   # boto3 reports a 404 here
        with open(Filename, "wb") as f:
            downloader.readinto(f)

    def delete_object(self, Bucket: str, Key: str, **_ignored) -> dict:  # noqa: N803
        from azure.core.exceptions import ResourceNotFoundError

        try:
            self._blob(Bucket, Key).delete_blob()
        except ResourceNotFoundError:
            pass                                  # S3 also answers "done" for a missing key
        return {"ResponseMetadata": {"HTTPStatusCode": 204}}

    def get_paginator(self, operation: str) -> _ListPaginator:
        if operation != "list_objects_v2":
            raise NotImplementedError(f"only list_objects_v2 is offered on Azure, not {operation}")
        return _ListPaginator(self)
