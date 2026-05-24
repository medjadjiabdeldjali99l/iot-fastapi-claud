"""Create (or update) the test user + ensure an org + profile exist.

Reads SUPABASE_URL / SUPABASE_SERVICE_KEY from backend/.env. Run from the
backend directory:

    .venv\\Scripts\\python.exe -m scripts.create_user

Re-runnable: if the user already exists, password and metadata are updated
and the profile is upserted."""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Allow `python scripts/create_user.py` as well as `python -m scripts.create_user`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from supabase import create_client  # noqa: E402

from app.core.config import settings  # noqa: E402

EMAIL = "abdeldjalil.medjadji@inkidia.dz"
PASSWORD = "djalil1234"
FULL_NAME = "Abdeldjalil Medjadji"
ROLE = "admin"
ORG_NAME = "Default Factory"
ORG_SLUG = "default"


def _ensure_org(admin) -> dict:
    res = (
        admin.table("organizations")
        .select("id, slug, name")
        .eq("slug", ORG_SLUG)
        .limit(1)
        .execute()
    )
    if res.data:
        return res.data[0]

    res = (
        admin.table("organizations")
        .insert({"name": ORG_NAME, "slug": ORG_SLUG})
        .execute()
    )
    return res.data[0]


def _find_user_by_email(admin, email: str):
    """list_users returns a list of User in supabase-py v2."""
    try:
        users = admin.auth.admin.list_users()
    except Exception as exc:
        print(f"⚠ list_users failed: {exc}")
        return None
    # Normalize to a flat iterable
    if hasattr(users, "users"):
        users = users.users
    for u in users or []:
        u_email = getattr(u, "email", None) or (u.get("email") if isinstance(u, dict) else None)
        if u_email and u_email.lower() == email.lower():
            return u
    return None


def _upsert_profile(admin, user_id: str, org_id: str) -> None:
    """The handle_new_user trigger inserts a profile only on auth.users INSERT
    — when we update an existing user, we have to upsert the profile ourselves."""
    res = (
        admin.table("profiles")
        .select("id")
        .eq("id", user_id)
        .limit(1)
        .execute()
    )
    payload = {
        "id": user_id,
        "organization_id": org_id,
        "full_name": FULL_NAME,
        "role": ROLE,
    }
    if res.data:
        admin.table("profiles").update(payload).eq("id", user_id).execute()
        print(f"→ Updated profile for {EMAIL}")
    else:
        admin.table("profiles").insert(payload).execute()
        print(f"→ Inserted profile for {EMAIL}")


def main() -> int:
    if not settings.SUPABASE_URL or not settings.SUPABASE_SERVICE_KEY:
        print("ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in backend/.env")
        return 1

    admin = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)

    org = _ensure_org(admin)
    print(f"→ Org: {org['name']} ({org['id']})")

    user_metadata = {
        "organization_id": org["id"],
        "full_name": FULL_NAME,
        "role": ROLE,
    }

    # Try to create; if it fails because the user exists, update instead.
    user_id = None
    try:
        res = admin.auth.admin.create_user(
            {
                "email": EMAIL,
                "password": PASSWORD,
                "email_confirm": True,
                "user_metadata": user_metadata,
            }
        )
        user = getattr(res, "user", None) or res
        user_id = getattr(user, "id", None) or user["id"]
        print(f"→ Created user: {EMAIL} ({user_id})")
        # Trigger handle_new_user already inserted the profile on INSERT,
        # but we upsert defensively in case the trigger wasn't applied.
        _upsert_profile(admin, user_id, org["id"])
        return 0
    except Exception as exc:
        msg = str(exc).lower()
        is_dupe = any(
            kw in msg for kw in ("already", "exists", "duplicate", "registered")
        )
        if not is_dupe:
            print(f"⚠ create_user failed: {exc}")
            print("  Looking up existing user anyway…")

    existing = _find_user_by_email(admin, EMAIL)
    if not existing:
        print(f"ERROR: could not find user {EMAIL} to update")
        return 2
    user_id = getattr(existing, "id", None) or existing["id"]
    print(f"→ User exists: {user_id}")

    admin.auth.admin.update_user_by_id(
        user_id,
        {"password": PASSWORD, "user_metadata": user_metadata},
    )
    print(f"→ Updated password and metadata for {EMAIL}")

    _upsert_profile(admin, user_id, org["id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
