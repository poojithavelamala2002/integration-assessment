# slack.py

#from fastapi import Request
# hubspot.py

# backend/integrations/hubspot.py

import os
import json
import base64
import secrets
import time
import asyncio
from urllib.parse import urlencode

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
import httpx

from integrations.integration_item import IntegrationItem
from redis_client import add_key_value_redis, get_value_redis, delete_key_redis

router = APIRouter(prefix="/integrations/hubspot", tags=["HubSpot"])

# --- Config (use env vars in real deployments) ---
CLIENT_ID = os.getenv("HUBSPOT_CLIENT_ID", "4eec587a-cd91-463e-89dc-4c94e5e6f159")
CLIENT_SECRET = os.getenv("HUBSPOT_CLIENT_SECRET", "60f0bed8-d6f3-4c3c-9b34-ed46d04e3f78")
REDIRECT_URI = os.getenv("HUBSPOT_REDIRECT_URI", "http://localhost:8000/integrations/hubspot/oauth2callback")

SCOPES = [
    "crm.objects.contacts.read",
    "crm.objects.companies.read",
    "crm.objects.contacts.write",
    "crm.objects.deals.read",
]

AUTH_BASE = "https://app.hubspot.com/oauth/authorize"
TOKEN_URL = "https://api.hubapi.com/oauth/v1/token"
CONTACTS_URL = "https://api.hubapi.com/crm/v3/objects/contacts"
COMPANIES_URL = "https://api.hubapi.com/crm/v3/objects/companies"
DEALS_URL = "https://api.hubapi.com/crm/v3/objects/deals"

# TTLs (seconds) - increase for testing; in prod, persist in DB
CREDENTIALS_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days
STATE_TTL_SECONDS = 60 * 15  # 15 minutes

# -- Helpers -----------------------------------------------------------------

def _encode_state(state_obj: dict) -> str:
    """Encode JSON state as URL-safe base64 string for transport in 'state' param."""
    raw = json.dumps(state_obj).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("utf-8")


def _decode_state(encoded_state: str) -> dict:
    try:
        raw = base64.urlsafe_b64decode(encoded_state.encode("utf-8"))
        return json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid state payload.")


def build_authorization_url(encoded_state: str) -> str:
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": " ".join(SCOPES),
        "state": encoded_state,
    }
    return f"{AUTH_BASE}?{urlencode(params)}"


async def _save_credentials_redis(key: str, token_json: dict):
    # Add expires_at timestamp for easy refresh checking
    expires_in = int(token_json.get("expires_in", 0))
    token_json["expires_at"] = int(time.time()) + expires_in if expires_in > 0 else None
    await add_key_value_redis(key, json.dumps(token_json), expire=CREDENTIALS_TTL_SECONDS)
    print(f"[hubspot] Saved credentials to redis key={key} (expires_in={expires_in})")


async def _refresh_tokens_if_needed(key: str, credentials: dict) -> dict:
    """
    If credentials include refresh_token and access_token expired (or about to),
    refresh them and update Redis. Returns fresh credentials dict.
    """
    expires_at = credentials.get("expires_at")
    now = int(time.time())
    # Refresh if expires_at is missing or token is expiring in next 60 seconds
    if expires_at is None or (now + 60 >= expires_at):
        refresh_token = credentials.get("refresh_token")
        if not refresh_token:
            raise HTTPException(status_code=401, detail="No refresh_token available, re-auth required.")
        async with httpx.AsyncClient(timeout=30) as client:
            data = {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
            }
            resp = await client.post(TOKEN_URL, data=data)
            if resp.status_code >= 400:
                raise HTTPException(status_code=resp.status_code, detail=f"Refresh token failed: {resp.text}")
            refreshed = resp.json()
            # preserve any fields we had and update with new tokens
            credentials.update(refreshed)
            # compute new expires_at and save back to redis
            expires_in = int(refreshed.get("expires_in", 0))
            credentials["expires_at"] = int(time.time()) + expires_in if expires_in > 0 else None
            await add_key_value_redis(key, json.dumps(credentials), expire=CREDENTIALS_TTL_SECONDS)
            print(f"[hubspot] Refreshed access token for key={key}")
    return credentials


# --- Public functions used by the rest of the codebase -----------------------

async def authorize_hubspot(user_id: str, org_id: str) -> str:
    """
    Returns the HubSpot OAuth2 authorization URL and stores encoded state in Redis for CSRF protection.
    """
    state_data = {
        "nonce": secrets.token_urlsafe(32),
        "user_id": user_id,
        "org_id": org_id,
    }
    encoded_state = _encode_state(state_data)
    # Save the encoded_state in redis keyed by org+user for later verification
    await add_key_value_redis(f"hubspot_state:{org_id}:{user_id}", encoded_state, expire=STATE_TTL_SECONDS)
    print("[DEBUG saved-state-key]", f"hubspot_state:{org_id}:{user_id}")
    print("[DEBUG saved-state-value]", encoded_state)
    return build_authorization_url(encoded_state)


@router.get("/authorize")
async def authorize_endpoint(user_id: str, org_id: str):
    """
    Frontend calls this to get the redirect URL to HubSpot's consent screen.
    Returns JSON with the authorize_url so the frontend can redirect/open a popup.
    """
    try:
        url = await authorize_hubspot(user_id, org_id)
        return {"authorize_url": url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to start authorization: {e}")


@router.get("/oauth2callback")
async def oauth2callback_hubspot(request: Request):
    """
    HubSpot redirects the user here after consent.
    Exchanges code for tokens and stores them in Redis.
    Closes the popup/tab after success.
    """
    error = request.query_params.get("error")
    if error:
        raise HTTPException(status_code=400, detail=error)

    code = request.query_params.get("code")
    encoded_state = request.query_params.get("state")
    print("[DEBUG callback-code]", code)
    print("[DEBUG callback-state]", encoded_state)

    if not code or not encoded_state:
        raise HTTPException(status_code=400, detail="Missing code or state.")

    # Decode state and validate
    state_data = _decode_state(encoded_state)
    nonce = state_data.get("nonce")
    user_id = state_data.get("user_id")
    org_id = state_data.get("org_id")
    if not (nonce and user_id and org_id):
        raise HTTPException(status_code=400, detail="Incomplete state.")

    # Verify stored state
    stored = await get_value_redis(f"hubspot_state:{org_id}:{user_id}")
    print("[DEBUG callback-state]", encoded_state)
    print("[DEBUG stored-state]", stored)
    if not stored:
        raise HTTPException(status_code=400, detail="State not found or expired.")
    # Decode bytes to string if necessary
    if isinstance(stored, bytes):
       stored = stored.decode("utf-8")
    print("[DEBUG stored-state decoded]", stored)    
    # stored contains the encoded_state we saved earlier
    if stored != encoded_state:
        raise HTTPException(status_code=400, detail="State does not match.")

    # Exchange code for token. Delete stored state only after success.
    async with httpx.AsyncClient(timeout=30) as client:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        resp = await client.post(TOKEN_URL, data=data, headers=headers)
        if resp.status_code >= 400:
            # do not delete state if token exchange fails
            raise HTTPException(status_code=resp.status_code, detail=f"Token exchange failed: {resp.text}")
        token_json = resp.json()

    # Persist credentials (with expires_at) and then remove the ephemeral state
    creds_key = f"hubspot_credentials:{org_id}:{user_id}"
    await _save_credentials_redis(creds_key, token_json)
    await delete_key_redis(f"hubspot_state:{org_id}:{user_id}")

    # UI: close popup/window; optionally parent window can poll for credentials/items
    close_window_script = """
    <html>
        <body>
            <script>
                try {
                    window.close();
                } catch (e) {
                    // fallback: show message
                    document.body.innerText = "You can close this window.";
                }
            </script>
            <p>Authentication completed. You can close this window.</p>
        </body>
    </html>
    """
    return HTMLResponse(content=close_window_script)


async def get_hubspot_credentials(user_id: str, org_id: str) -> dict:
    """
    Retrieve credentials from Redis and refresh if needed.
    Does NOT delete the credentials — they persist for reuse.
    Returns a dict with at minimum 'access_token' (and possibly refreshed tokens).
    """
    key = f"hubspot_credentials:{org_id}:{user_id}"
    credentials_raw = await get_value_redis(key)
    if not credentials_raw:
        raise HTTPException(status_code=400, detail="No credentials found. Please connect HubSpot first.")
    try:
        credentials = json.loads(credentials_raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Stored credentials are corrupted.")

    # Refresh if necessary and update Redis
    credentials = await _refresh_tokens_if_needed(key, credentials)
    return credentials


def create_integration_item_metadata_object(response_json: dict, obj_type: str = "contact") -> IntegrationItem:
    """
    Creates an IntegrationItem for a HubSpot object (contact/company/deal).
    The IntegrationItem constructor here follows the fields used in your project
    (id, type, name, creation_time, last_modified_time, parent_id). Adjust if needed.
    """
    props = response_json.get("properties", {}) or {}

    if obj_type == "contact":
        name = f"{props.get('firstname', '')} {props.get('lastname', '')}".strip() or props.get("email") or f"id:{response_json.get('id')}"
        creation_time = props.get("createdate") or response_json.get("createdAt")
        updated_time = props.get("hs_lastmodifieddate") or response_json.get("updatedAt")
    elif obj_type == "company":
        name = props.get("name") or props.get("domain") or f"company:{response_json.get('id')}"
        creation_time = props.get("createdate") or response_json.get("createdAt")
        updated_time = props.get("hs_lastmodifieddate") or response_json.get("updatedAt")
    elif obj_type == "deal":
        name = props.get("dealname") or f"deal:{response_json.get('id')}"
        creation_time = props.get("createdate") or response_json.get("createdAt")
        updated_time = props.get("hs_lastmodifieddate") or response_json.get("updatedAt")
    else:
        name = response_json.get("id")
        creation_time = response_json.get("createdAt")
        updated_time = response_json.get("updatedAt")

    # Construct IntegrationItem with expected fields; adjust to match your IntegrationItem signature
    item = IntegrationItem(
        id=str(response_json.get("id")),
        type=obj_type,
        name=name,
        creation_time=creation_time,
        last_modified_time=updated_time,
        parent_id=None,
    )

    # If IntegrationItem supports additional parameters or properties, you can attach them:
    try:
        # Attach dynamic attributes if allowed (non-intrusive)
        setattr(item, "raw_properties", props)
    except Exception:
        pass

    return item


async def _fetch_objects(client: httpx.AsyncClient, url: str, headers: dict, params: dict = None, max_items: int = 200):
    """
    Generic helper that follows simple pagination for HubSpot CRM v3 endpoints.
    Returns a flat list of object JSONs (up to max_items).
    """
    collected = []
    cur_params = params.copy() if params else {}
    while True:
        resp = await client.get(url, headers=headers, params=cur_params)
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=f"Error fetching {url}: {resp.text}")
        data = resp.json()
        results = data.get("results", []) or []
        collected.extend(results)
        # stop if we've reached max_items
        if len(collected) >= max_items:
            return collected[:max_items]
        # Check paging
        paging = data.get("paging", {})
        next_link = None
        if paging:
            next_link = paging.get("next", {}).get("link")
        if not next_link:
            break
        # HubSpot returns a full link for next; use it directly (no params)
        url = next_link
        cur_params = None  # next_link already includes params
    return collected


async def get_items_hubspot(credentials: dict, max_items_per_type: int = 100):
    """
    Fetch contacts, companies, and deals using the provided credentials dict.
    Returns a list of IntegrationItem objects.
    """
    access_token = credentials.get("access_token")
    if not access_token:
        raise HTTPException(status_code=400, detail="Missing access_token in credentials.")

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }

    items = []
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            # Contacts - request explicit properties for consistency
            contacts_params = {"limit": 100, "properties": "firstname,lastname,email,phone,createdate,hs_lastmodifieddate"}
            contacts = await _fetch_objects(client, CONTACTS_URL, headers, params=contacts_params, max_items=max_items_per_type)
            for c in contacts:
                items.append(create_integration_item_metadata_object(c, obj_type="contact"))

            # Companies
            companies_params = {"limit": 100, "properties": "name,domain,phone,createdate,hs_lastmodifieddate"}
            companies = await _fetch_objects(client, COMPANIES_URL, headers, params=companies_params, max_items=max_items_per_type)
            for co in companies:
                items.append(create_integration_item_metadata_object(co, obj_type="company"))

            # Deals
            deals_params = {"limit": 100, "properties": "dealname,amount,dealstage,createdate,hs_lastmodifieddate"}
            deals = await _fetch_objects(client, DEALS_URL, headers, params=deals_params, max_items=max_items_per_type)
            for d in deals:
                items.append(create_integration_item_metadata_object(d, obj_type="deal"))

        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"HubSpot API fetch failed: {e}")

    return items


@router.get("/items")
async def get_items_endpoint(user_id: str, org_id: str):
    """
    Frontend calls this AFTER the OAuth popup closes.
    It pulls tokens from Redis (and refreshes them if needed) and returns the integration items.
    """
    creds = await get_hubspot_credentials(user_id, org_id)
    items = await get_items_hubspot(creds)
    # Serialize IntegrationItem objects - try dataclass or __dict__ fallback
    serialized = []
    for item in items:
        if hasattr(item, "to_dict"):
            serialized.append(item.to_dict())
        elif hasattr(item, "__dict__"):
            serialized.append({k: v for k, v in item.__dict__.items() if not k.startswith("_")})
        else:
            # last resort: convert via str()
            serialized.append({"id": getattr(item, "id", str(item)), "repr": str(item)})

    return JSONResponse(content={"items": serialized})

