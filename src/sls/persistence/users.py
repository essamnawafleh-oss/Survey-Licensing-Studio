"""Local user accounts, workflow roles and the audit log.

Roles gate *workflow actions* inside the application. They are not a security boundary:
anyone with file-system access to the data directory can read or alter the database.
"""
from __future__ import annotations

import getpass
import hashlib
import hmac
import json
import os
from dataclasses import dataclass

from ..core.errors import PermissionDeniedError, ValidationError
from .db import Database, new_id, now_iso

ROLES = ("surveyor", "reviewer", "admin")
_PBKDF2_ITERATIONS = 240_000

# action -> roles allowed
PERMISSIONS: dict[str, tuple[str, ...]] = {
    "edit_case": ROLES,
    "edit_drawing": ROLES,
    "edit_license": ROLES,
    "run_calculation": ROLES,
    "generate_report": ROLES,
    "import_rules": ("reviewer", "admin"),
    "verify_rules": ("reviewer", "admin"),
    "verify_units": ROLES,
    "verify_boundary": ROLES,
    "verify_license": ("reviewer", "admin"),
    "review_report": ("reviewer", "admin"),
    "approve_report": ("reviewer", "admin"),
    "manage_users": ("admin",),
    "restore_backup": ("admin",),
}


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt_hex, dk_hex = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations))
    return hmac.compare_digest(dk.hex(), dk_hex)


@dataclass(frozen=True)
class User:
    id: str
    username: str
    display_name: str
    role: str

    def can(self, action: str) -> bool:
        return self.role in PERMISSIONS.get(action, ())

    def require(self, action: str) -> None:
        if not self.can(action):
            raise PermissionDeniedError(
                f"User '{self.username}' (role {self.role}) is not permitted to {action}.",
                action=action,
            )


def single_user_mode_user() -> User:
    """Used when access control is disabled: the OS account acts with admin role."""
    try:
        name = getpass.getuser()
    except (OSError, KeyError):
        name = "local"
    return User(id="local", username=name, display_name=name, role="admin")


class UserRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(self, username: str, password: str, role: str, display_name: str = "") -> User:
        username = username.strip()
        if not username:
            raise ValidationError("Username is required.")
        if role not in ROLES:
            raise ValidationError(f"Invalid role: {role}")
        if len(password) < 8:
            raise ValidationError("Password must be at least 8 characters.")
        uid = new_id()
        with self.db.transaction():
            self.db.execute(
                "INSERT INTO users(id,username,display_name,role,password_hash,created_at) VALUES (?,?,?,?,?,?)",
                (uid, username, display_name or username, role, hash_password(password), now_iso()),
            )
        return User(uid, username, display_name or username, role)

    def authenticate(self, username: str, password: str) -> User | None:
        row = self.db.query_one("SELECT * FROM users WHERE username=? AND active=1", (username,))
        if row is None or not check_password(password, row["password_hash"]):
            return None
        return User(row["id"], row["username"], row["display_name"], row["role"])

    def list(self) -> list[User]:
        return [User(r["id"], r["username"], r["display_name"], r["role"])
                for r in self.db.query("SELECT * FROM users WHERE active=1 ORDER BY username")]

    def count(self) -> int:
        return int(self.db.query_one("SELECT COUNT(*) FROM users WHERE active=1")[0])


class AuditLog:
    def __init__(self, db: Database) -> None:
        self.db = db

    def record(self, user: User, action: str, entity_type: str, entity_id: str = "",
               case_id: str | None = None, **details: object) -> None:
        self.db.execute(
            "INSERT INTO audit_log(ts,username,action,entity_type,entity_id,case_id,details_json)"
            " VALUES (?,?,?,?,?,?,?)",
            (now_iso(), user.username, action, entity_type, entity_id, case_id,
             json.dumps(details, ensure_ascii=False, sort_keys=True, default=str)),
        )

    def for_case(self, case_id: str) -> list[dict[str, object]]:
        rows = self.db.query("SELECT * FROM audit_log WHERE case_id=? ORDER BY id", (case_id,))
        return [dict(r) for r in rows]
