"""Per-worker blob cache with server-side schedule evaluation."""

import asyncio
import hashlib
import json
import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlsplit

from azure.core import MatchConditions
from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import AzureError, ResourceNotFoundError
from azure.storage.blob.aio import BlobClient
from pydantic import ValidationError

from .models import NotificationDocument

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NotificationSettings:
    enabled: bool = False
    account_url: str = ""
    container: str = "notifications"
    blob: str = "notifications.json"
    cache_seconds: int = 30
    poll_seconds: int = 60
    timeout_seconds: int = 3

    @classmethod
    def from_env(cls):
        enabled = os.getenv("NOTIFICATIONS_ENABLED", "false").lower()
        if enabled not in ("true", "false"):
            raise ValueError("NOTIFICATIONS_ENABLED must be true or false")
        if enabled == "false":
            return cls()
        url = os.getenv("NOTIFICATIONS_STORAGE_ACCOUNT_URL", "").rstrip("/")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
            or "\\" in url
            or any(ord(c) <= 32 for c in url)
        ):
            raise ValueError("NOTIFICATIONS_STORAGE_ACCOUNT_URL must be an HTTPS account endpoint without a SAS token")
        container = os.getenv("NOTIFICATIONS_STORAGE_CONTAINER", "notifications")
        blob = os.getenv("NOTIFICATIONS_STORAGE_BLOB", "notifications.json")
        if not container or not blob:
            raise ValueError("Notification container and blob names must not be empty")

        def number(name: str, default: int, maximum: int) -> int:
            value = int(os.getenv(name, str(default)))
            if not 1 <= value <= maximum:
                raise ValueError(f"{name} must be between 1 and {maximum}")
            return value

        # The app's /content route serves ingestion/upload containers. Never put
        # the authoring document in any of those containers, even on another account.
        public_containers = {
            os.getenv("AZURE_STORAGE_CONTAINER"),
            os.getenv("AZURE_USERSTORAGE_CONTAINER"),
            os.getenv("AZURE_IMAGESTORAGE_CONTAINER"),
        }
        if container in public_containers:
            raise ValueError("Notification storage must use a dedicated container outside document ingestion")
        return cls(
            enabled=True,
            account_url=url,
            container=container,
            blob=blob,
            cache_seconds=number("NOTIFICATIONS_CACHE_SECONDS", 30, 3600),
            poll_seconds=number("NOTIFICATIONS_POLL_SECONDS", 60, 3600),
            timeout_seconds=number("NOTIFICATIONS_TIMEOUT_SECONDS", 3, 30),
        )


def active_payload(document: Optional[NotificationDocument], now: datetime, poll_seconds: int) -> dict[str, Any]:
    """Only emit presentation fields. All intervals use inclusive end boundaries."""
    active = []
    seen = set()
    refresh_ms = poll_seconds * 1000
    validity_ms = poll_seconds * 2000
    if document:
        for notification in sorted(document.notifications, key=lambda item: -item.priority):
            if not notification.enabled:
                continue
            matches = notification.schedule is None
            if notification.schedule:
                start, end = notification.schedule.interval()
                if start <= now <= end:
                    matches = True
                # The first microsecond after end is the first inactive instant.
                for boundary in (start, end + timedelta(microseconds=1)):
                    if boundary > now:
                        delay = max(1, math.ceil((boundary - now).total_seconds() * 1000))
                        refresh_ms = min(refresh_ms, delay)
                        validity_ms = min(validity_ms, delay)
            if matches:
                # Derive dismissal identity from content, so authors need no ID or revision.
                # Sorting JSON keys keeps this stable when entries/properties are reordered.
                key = hashlib.sha256(
                    json.dumps(notification.model_dump(mode="json"), sort_keys=True).encode("utf-8")
                ).hexdigest()
                if key in seen:
                    continue
                seen.add(key)
                active.append(
                    {
                        "key": key,
                        "type": notification.type,
                        "title": notification.title,
                        "message": notification.message,
                        "dismissible": notification.dismissible,
                    }
                )
    return {"notifications": active, "refreshAfterMs": refresh_ms, "validForMs": validity_ms}


class NotificationService:
    def __init__(self, settings: NotificationSettings, blob: Optional[BlobClient] = None):
        self.settings = settings
        self.blob = blob
        self.document: Optional[NotificationDocument] = None
        self.etag: Optional[str] = None
        self.next_refresh = 0.0
        self.lock = asyncio.Lock()

    @classmethod
    def create(cls, settings: NotificationSettings, credential: AsyncTokenCredential):
        blob = None
        if settings.enabled:
            blob = BlobClient(
                account_url=settings.account_url,
                container_name=settings.container,
                blob_name=settings.blob,
                credential=credential,
                retry_total=1,
                connection_timeout=settings.timeout_seconds,
                read_timeout=settings.timeout_seconds,
            )
        return cls(settings, blob)

    async def refresh(self):
        if not self.blob:
            return
        # HEAD avoids downloading unchanged files. Match GET to that version so
        # a concurrent upload cannot put different content under the cached ETag.
        properties = await self.blob.get_blob_properties()
        if self.etag == properties.etag and self.document is not None:
            return
        download = await self.blob.download_blob(
            etag=properties.etag,
            match_condition=MatchConditions.IfNotModified,
        )
        raw = await download.readall()
        self.document = NotificationDocument.model_validate_json(raw)
        self.etag = properties.etag

    async def get_payload(self):
        if self.blob and time.monotonic() >= self.next_refresh:
            async with self.lock:
                if time.monotonic() >= self.next_refresh:
                    try:
                        await asyncio.wait_for(self.refresh(), timeout=self.settings.timeout_seconds)
                    except (AzureError, asyncio.TimeoutError, ValidationError, ValueError) as exc:
                        # Do not keep obsolete outage notices after a failed update.
                        self.document = None
                        self.etag = None
                        # Avoid logging source JSON, credentials or SDK request URLs.
                        if isinstance(exc, ResourceNotFoundError):
                            logger.warning("Notification document unavailable: not found")
                        else:
                            logger.warning("Notification refresh failed (%s); serving no banners", type(exc).__name__)
                    finally:
                        self.next_refresh = time.monotonic() + self.settings.cache_seconds
        return active_payload(self.document, datetime.now(timezone.utc), self.settings.poll_seconds)

    async def close(self):
        if self.blob:
            await self.blob.close()
