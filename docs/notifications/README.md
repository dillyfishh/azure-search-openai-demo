# Notification banners

The backend reads a private Azure Blob Storage JSON file, validates it, and returns
only active banners through `GET /notifications`. The source file and its schedules
never reach the browser. The feature is disabled by default.

## Authoring

```json
{
  "schemaVersion": 1,
  "notifications": [
    {
      "type": "warning",
      "title": "Scheduled maintenance",
      "message": "Search may be unavailable. See [More Information](https://moreinformation.com) for updates.",
      "dismissible": true,
      "schedule": {
        "startDate": "2030-09-20",
        "startTime": "06:00",
        "endDate": "2030-09-22",
        "endTime": "22:00"
      }
    }
  ]
}
```

This banner starts at **06:00 on 20 September** and remains visible continuously,
including overnight, until **22:00 on 22 September**, in Sydney time. Start and end
are inclusive. It does not turn off and on each day.

| Entry field | Meaning |
| --- | --- |
| `message` | Required Markdown text, up to **40,000 characters**. |
| `type` | `info` (default), `success`, `warning`, or `error`. |
| `title` | Optional plain text heading, up to 160 characters. |
| `dismissible` | Defaults to `false`. If true, users can hide the notice for the current browser tab session. |
| `enabled` | Defaults to `true`; set `false` to keep a draft. |
| `priority` | Optional 0–100, default 0. Higher numbers appear first; ties follow file order. |
| `schedule` | Optional single object. Omit it to show the notice until removed or disabled. |

Each schedule requires `startDate`, `startTime`, `endDate`, and `endTime`. Use dates
like `2030-09-20` and local times like `06:00` or `06:00:00`. Add `timezone`, for example
`"timezone": "Europe/London"`, to override the default **`Australia/Sydney`**.
Times must not contain UTC offsets. The end must be on or after the start.

Daylight-saving changes follow the selected IANA timezone. A nonexistent local time
is rejected with a validation error. During a repeated hour, starts use the first
occurrence and ends the second. Backend time is authoritative.

There are **no author-supplied IDs, revisions, or actions**. Editing a notification
makes it visible again after dismissal. Reordering it does not reset dismissal.
Identical duplicates are displayed once. No separate action button is needed:
place links anywhere in `message` using `[link text](https://example.com)`.

Markdown supports links, emphasis, paragraphs, lists, and code. Links may use HTTPS
or same-site `/paths`. Raw HTML, images, and unsafe link destinations are not rendered.
`<a>` tags are not supported; use Markdown links. Authored text is displayed as written;
component labels use the existing UI translations.

The document allows up to 1,000 entries. Unknown fields, invalid types, and malformed
schedules reject the whole document. Associate `notifications.schema.json` in your
editor for completion; do not add `$schema` to the data file. Validate before upload:

```sh
source .venv/bin/activate
PYTHONPATH=app/backend python -m notifications.validate path/to/notifications.json
```

Use `notifications.example.json` as a starting point. To clear every banner, upload
`{"schemaVersion":1,"notifications":[]}`.

## Storage and environment setup

1. Use a **dedicated private container**, separate from content, image, and upload
   containers. Do not ingest the JSON into Search or put it in `data/` or `static/`.
   Document-container names are rejected at startup to prevent `/content` exposing it.
2. Grant the backend managed identity **Storage Blob Data Reader** on the container
   or account. Grant editors Blob Data Contributor separately. Local development uses
   the existing Azure Developer CLI credential. No storage keys or SAS tokens are needed.
   Ensure the backend can reach the account through its firewall/private endpoint and DNS.
3. Set the environment variables, then use the normal provisioning/deployment process:

   ```sh
   azd env set NOTIFICATIONS_ENABLED true
   azd env set NOTIFICATIONS_STORAGE_ACCOUNT_URL https://YOUR_ACCOUNT.blob.core.windows.net
   azd env set NOTIFICATIONS_STORAGE_CONTAINER notifications
   azd env set NOTIFICATIONS_STORAGE_BLOB notifications.json
   ```

4. Upload the validated file in one operation:

   ```sh
   az storage blob upload --auth-mode login --account-name YOUR_ACCOUNT \
     --container-name notifications --name notifications.json \
     --file path/to/notifications.json --overwrite
   ```

Storage provisioning, role assignments, and upload remain explicit setup steps. For
rollback, use blob versioning/soft delete. Use conditional uploads if several editors
may change the file. Treat active banner text as public service information: the API
has the same application-level access policy as `/config`; hosting Easy Auth still applies.

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `NOTIFICATIONS_ENABLED` | `false` | Enable/disable the feature. |
| `NOTIFICATIONS_STORAGE_ACCOUNT_URL` | None | Required when enabled: HTTPS blob account endpoint without path/query/SAS. |
| `NOTIFICATIONS_STORAGE_CONTAINER` | `notifications` | Private container. |
| `NOTIFICATIONS_STORAGE_BLOB` | `notifications.json` | Blob name, including an optional folder prefix. |
| `NOTIFICATIONS_CACHE_SECONDS` | `30` | Per-worker storage refresh interval; 1–3600 seconds. |
| `NOTIFICATIONS_POLL_SECONDS` | `60` | Browser refresh interval; 1–3600 seconds. Shortened at schedule boundaries. |
| `NOTIFICATIONS_TIMEOUT_SECONDS` | `3` | Total storage refresh timeout; 1–30 seconds. |

All seven settings are mapped through Bicep and both deployment pipelines. Invalid
configuration while enabled fails startup. There is no document byte-limit setting.

## Operation and reuse

The backend caches the validated document, uses ETags to avoid redundant downloads,
and evaluates the schedule on every request. Concurrent refreshes are coalesced and
storage operations have a timeout. The browser receives only `key`, `type`, `title`,
`message`, and `dismissible` for active entries, plus refresh/expiry durations. `key`
is generated automatically from the normalized entry to support stable dismissals.
It is not a field that notification authors maintain.

The response uses `Cache-Control: no-store`. Missing/invalid files or storage failures
log a warning and yield an empty list without breaking chat. The browser clears stale
notices on expiry or fetch failure and refreshes after returning to a tab. Edits may
take approximately cache interval + poll interval + request latency to propagate.
Browser suspension can delay timers. Monitor `notifications.service` warnings; an empty
HTTP 200 response alone does not prove storage is healthy.

- Backend: `app/backend/notifications/`; setup, route and shutdown in `app/backend/app.py`.
- Frontend: `app/frontend/src/components/NotificationBanners/`, already mounted in the shared layout.
- Reuse: mount `<NotificationBanners />` anywhere, or use `NotificationBannerList` with existing data.
  The component uses the existing React Markdown dependency; no new dependency is required.
- Backend reuse: construct `NotificationService` with settings and an Azure async credential,
  expose `get_payload()`, and call `close()` on shutdown.

## Migration from the initial branch design

Remove `id`, `revision`, and `action` from authored entries. Move action links into
`message` as Markdown. Replace the `schedules` array with one `schedule` object using
local start/end dates and times. Recurring windows and weekday rules are removed.
Remove `NOTIFICATIONS_MAX_BYTES` from environment/CI settings. Old documents must be
updated: the validator rejects removed fields. The schema version remains 1 because
this is a revision of the unshipped feature branch.

Regenerate the editor schema after changing models:

```sh
PYTHONPATH=app/backend python -m notifications.validate \
  docs/notifications/notifications.schema.json --write-schema
pytest tests/test_notifications.py tests/test_app.py
(cd app/frontend && npm run build)
pytest tests/e2e.py -k notification
```
