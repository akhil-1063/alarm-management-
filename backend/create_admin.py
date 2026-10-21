"""Controlled first-administrator creation.

Run:   py -m backend.create_admin
The password is prompted interactively (or passed via ICU_BOOTSTRAP_PASSWORD
for scripted setup). It is never hardcoded in the repository.
"""
from __future__ import annotations

import getpass
import os
import sys

from sqlalchemy import select

from backend.database import SessionLocal, init_db
from backend.models import User
from backend.security import audit, hash_password


def create_admin() -> User | None:
    init_db()
    db = SessionLocal()
    try:
        existing = db.execute(select(User).where(User.role == "admin")).scalars().first()
        if existing:
            print(f"Administrator already exists: {existing.email}")
            return None

        email = os.environ.get("ICU_BOOTSTRAP_EMAIL", "").strip() or input("Admin email: ").strip().lower()
        name = os.environ.get("ICU_BOOTSTRAP_NAME", "").strip() or input("Admin full name: ").strip()
        password = os.environ.get("ICU_BOOTSTRAP_PASSWORD", "")
        if not password:
            password = getpass.getpass("Admin password (min 10 chars): ")
            confirm = getpass.getpass("Confirm password: ")
            if password != confirm:
                print("Passwords do not match.", file=sys.stderr)
                sys.exit(1)
        if len(password) < 10:
            print("Password must be at least 10 characters.", file=sys.stderr)
            sys.exit(1)
        if not email or "@" not in email:
            print("A valid email is required.", file=sys.stderr)
            sys.exit(1)

        user = User(
            email=email,
            password_hash=hash_password(password),
            full_name=name or "Administrator",
            role="admin",
            is_active=True,
            is_approved=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        audit(db, "admin_created", actor_user_id=user.id, actor_label=user.email,
              entity_type="user", entity_id=user.id, detail="initial administrator")
        print(f"Administrator created: {user.email}")
        return user
    finally:
        db.close()


if __name__ == "__main__":
    create_admin()
