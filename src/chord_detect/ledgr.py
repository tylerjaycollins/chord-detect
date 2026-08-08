"""Push a finished chart into Ledgr as a song item.

No new API was needed on the Ledgr side. `POST /api/machine/items` already
takes a bare item object and routes it through the same `createItem` the in-app
editor uses, so this surface can't drift from Ledgr's own contract:

    { "type": "song",
      "title": "...",
      "body": { "format": "chordpro", "text": "..." } }

Two things about the credential are easy to get wrong, so they're checked
explicitly rather than surfacing as a bare 401:

  * The token must be **api**-scoped. Ledgr's `verifyApiToken` only accepts
    `api`; the `mcp`-scoped token used by the Claude connector is rejected.
  * Ledgr acts on a single owner (`resolveMachineOwner`). If `LEDGR_API_OWNER_UPN`
    isn't set on the server the request comes back 503, which says nothing about
    the token at all.

Stdlib `urllib` on purpose — one JSON POST doesn't justify a dependency.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# Note there are two live Ledgr hosts. `ledgr-teal` is what runbook.md
# references and is the one with OAuth discovery configured; `ledgr-sandy` is
# the current target. If an upload ever lands somewhere unexpected, check this
# first -- both hosts answer, so a wrong value fails by filing the song in the
# wrong instance rather than by erroring.
DEFAULT_URL = "https://ledgr-sandy.vercel.app"
SONG_TYPE = "song"
CHORDPRO_FORMAT = "chordpro"
TIMEOUT = 30


class LedgrError(RuntimeError):
    """Anything that stopped the upload, phrased for a human."""


@dataclass(frozen=True)
class Uploaded:
    """Where the song landed, so the caller can offer to open it."""

    item_id: str
    title: str
    url: str


CONFIG = Path.home() / ".config" / "overtone" / "config.json"


def _config() -> dict:
    """Settings from disk.

    A file rather than only an environment variable, because the macOS bundle
    is launched by Finder and inherits none of the shell's environment -- so an
    export in a shell profile works from a terminal and silently doesn't from
    the Dock icon.
    """
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return {}


def save_config(**values: str) -> Path:
    """Merge settings into the config file, creating it if needed."""
    current = _config()
    current.update({k: v for k, v in values.items() if v is not None})
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(current, indent=2) + "\n")
    # The token is a credential; don't leave it world-readable.
    CONFIG.chmod(0o600)
    return CONFIG


def base_url() -> str:
    """Ledgr's origin. `LEDGR_URL` points this at a dev server for testing."""
    return (
        os.environ.get("LEDGR_URL") or _config().get("url") or DEFAULT_URL
    ).rstrip("/")


def token() -> str | None:
    """The API token: environment first, then the config file."""
    return os.environ.get("LEDGR_API_TOKEN") or _config().get("token") or None


def configured() -> bool:
    """Whether an upload could even be attempted — gates the UI button."""
    return token() is not None


def upload_song(text: str, *, title: str, url: str | None = None,
                api_token: str | None = None) -> Uploaded:
    """Create a song item in Ledgr from ChordPro source.

    Always creates; it never updates an existing song. Re-uploading the same
    song therefore gives you a second item, which is the right default only
    while the chart still flows one way. Revisit when corrections start coming
    back from Ledgr.
    """
    origin = (url or base_url()).rstrip("/")
    secret = api_token or token()
    if not secret:
        raise LedgrError(
            "No Ledgr token. In the Ledgr repo run "
            "`node scripts/make-token.mjs overtone api`, put the printed entry "
            "in LEDGR_API_TOKENS, and set LEDGR_API_TOKEN to the raw lgr_ "
            "token it shows once. Note the env var holds a hash, not the "
            "token itself, so the two are different strings."
        )
    if not text.strip():
        raise LedgrError("Nothing to upload — the ChordPro source is empty.")

    # The batch shape (`{items: [...]}`) rather than a bare item. The route
    # accepts either, but this is the form Ledgr documents and the one its own
    # cron callers exercise, and it reports per-item errors instead of failing
    # the whole request opaquely.
    payload = json.dumps(
        {
            "items": [
                {
                    "type": SONG_TYPE,
                    "title": title,
                    "body": {"format": CHORDPRO_FORMAT, "text": text},
                }
            ]
        }
    ).encode()

    request = urllib.request.Request(
        f"{origin}/api/machine/items",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as err:
        raise LedgrError(_explain(err, origin)) from err
    except urllib.error.URLError as err:
        raise LedgrError(f"Couldn't reach Ledgr at {origin}: {err.reason}") from err

    item = _first_item(body)
    if item is None:
        raise LedgrError(f"Ledgr accepted the request but returned no item: {body}")

    item_id = str(item.get("id", ""))
    return Uploaded(
        item_id=item_id,
        title=str(item.get("title") or title),
        url=f"{origin}/items/{item_id}" if item_id else origin,
    )


def update_song(item_id: str, text: str, *, title: str, url: str | None = None,
                api_token: str | None = None) -> Uploaded:
    """Rewrite an existing Ledgr song in place.

    The counterpart to `upload_song`, so pushing a corrected chart replaces the
    chart rather than leaving a trail of near-identical songs to tidy up. Uses
    the same batch envelope as create; PATCH routes through the same
    `updateItem` the in-app editor uses.
    """
    origin = (url or base_url()).rstrip("/")
    secret = api_token or token()
    if not secret:
        raise LedgrError("No Ledgr token configured.")

    payload = json.dumps(
        {
            "items": [
                {
                    "id": item_id,
                    "title": title,
                    "body": {"format": CHORDPRO_FORMAT, "text": text},
                }
            ]
        }
    ).encode()

    request = urllib.request.Request(
        f"{origin}/api/machine/items",
        data=payload,
        method="PATCH",
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as err:
        raise LedgrError(_explain(err, origin)) from err
    except urllib.error.URLError as err:
        raise LedgrError(f"Couldn't reach Ledgr at {origin}: {err.reason}") from err

    rows = body.get("updated") if isinstance(body, dict) else None
    if not rows:
        # Reported per-item rather than as a status code, so surface it.
        errors = (body or {}).get("errors") if isinstance(body, dict) else None
        detail = errors[0].get("error") if errors else "no item was updated"
        raise LedgrError(f"Ledgr didn't update the song: {detail}")

    item = rows[0] if isinstance(rows[0], dict) else {}
    return Uploaded(
        item_id=str(item.get("id", item_id)),
        title=str(item.get("title") or title),
        url=f"{origin}/items/{item.get('id', item_id)}",
    )


def _first_item(body: object) -> dict | None:
    """Unwrap the response.

    A single create answers with the item; the batch shape answers with
    `{ count, created: [...], errors: [...] }`. Accept either rather than
    depending on which branch Ledgr took.
    """
    if not isinstance(body, dict):
        return None
    created = body.get("created")
    if isinstance(created, list) and created and isinstance(created[0], dict):
        return created[0]
    if "id" in body:
        return body
    item = body.get("item")
    return item if isinstance(item, dict) else None


def _explain(err: urllib.error.HTTPError, origin: str) -> str:
    """Turn Ledgr's terse status codes into something actionable.

    401 and 503 are the two that will actually happen during setup, and neither
    error body says what to do about it.
    """
    try:
        detail = json.loads(err.read().decode() or "{}").get("error", "")
    except Exception:
        detail = ""

    if err.code == 401:
        return (
            f"Ledgr rejected the token ({origin}). It must be an api-scoped "
            "entry in LEDGR_API_TOKENS — an mcp-scoped token is refused by "
            "verifyApiToken."
        )
    if err.code == 503:
        return (
            "Ledgr has no owner configured, so it can't file the song. Set "
            "LEDGR_API_OWNER_UPN on the server. (The token was fine.)"
        )
    if err.code == 400:
        return f"Ledgr rejected the song: {detail or 'bad request'}"
    return f"Ledgr returned {err.code}{f': {detail}' if detail else ''}"
