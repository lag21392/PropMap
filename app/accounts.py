from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from threading import Lock
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from . import store

COOKIE = "propmap_sid"
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{3,32}$")
EMAIL_RE = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}$")
HASHER = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2)
SESSION_DAYS = 30
VERIFY_MAX_AGE = 48 * 3600
_rate_lock = Lock()
_rate: dict[str, list[float]] = {}
_crypto_lock = Lock()
_fernet: Fernet | None = None
_hmac_key: bytes | None = None
_dummy_hash: str | None = None


@dataclass
class Account:
    id: str
    username: str
    email_masked: str
    email_verified: bool
    created_at: str


def init_tables() -> None:
    with store.connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY,
                username_hmac TEXT NOT NULL UNIQUE,
                username_enc TEXT NOT NULL,
                email_hmac TEXT NOT NULL UNIQUE,
                email_enc TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                email_verified INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                verified_at TEXT DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS account_sessions (
                token_hmac TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS account_vaults (
                user_id TEXT PRIMARY KEY,
                cipher TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON account_sessions(user_id)")
        conn.commit()


def reset_crypto() -> None:
    global _fernet, _hmac_key, _dummy_hash
    _fernet = None
    _hmac_key = None
    _dummy_hash = None


def public_url() -> str:
    return (os.environ.get("APP_PUBLIC_URL") or "http://127.0.0.1:8000").rstrip("/")


def smtp_configured() -> bool:
    return bool((os.environ.get("SMTP_HOST") or "").strip())


def mail_address() -> str:
    raw = (os.environ.get("MAIL_ADDRESS") or "").strip()
    if raw:
        return raw
    from_addr = (os.environ.get("SMTP_FROM") or "cuenta@propmap.local").strip()
    match = re.search(r"<([^>]+)>", from_addr)
    return (match.group(1) if match else from_addr).strip()


def mail_inbox_url() -> str:
    return (os.environ.get("MAIL_UI_URL") or "").strip()


def reveal_verify_link() -> bool:
    flag = (os.environ.get("AUTH_DEV_SHOW_LINK") or "").strip().lower()
    if flag in {"1", "true", "yes"}:
        return True
    return not smtp_configured()


def _keys() -> tuple[Fernet, bytes]:
    global _fernet, _hmac_key
    with _crypto_lock:
        if _fernet is not None and _hmac_key is not None:
            return _fernet, _hmac_key
        secret = (os.environ.get("AUTH_SECRET") or "").strip()
        if not secret:
            path = store.DATA_DIR / "auth.key"
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                secret = path.read_text(encoding="utf-8").strip()
            else:
                secret = secrets.token_hex(32)
                path.write_text(secret + "\n", encoding="utf-8")
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
        raw = hashlib.sha256(b"propmap-auth:" + secret.encode("utf-8")).digest()
        _hmac_key = hashlib.sha256(b"hmac:" + raw).digest()
        fernet_key = base64.urlsafe_b64encode(hashlib.sha256(b"fernet:" + raw).digest())
        _fernet = Fernet(fernet_key)
        return _fernet, _hmac_key


def _encrypt(plain: str) -> str:
    token = _keys()[0].encrypt(plain.encode("utf-8"))
    return token.decode("ascii")


def _decrypt(token: str) -> str:
    try:
        return _keys()[0].decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise HTTPException(500, "No se pudieron leer los datos cifrados") from exc


def _hmac(value: str) -> str:
    key = _keys()[1]
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(when: datetime | None = None) -> str:
    return (when or _now()).isoformat()


def normalize_username(raw: str) -> str:
    return (raw or "").strip()


def normalize_email(raw: str) -> str:
    return (raw or "").strip().lower()


def mask_email(email: str) -> str:
    local, _, domain = (email or "").partition("@")
    if not local or not domain:
        return "***"
    host, _, tld = domain.partition(".")
    local_bit = local[0] + "***"
    host_bit = (host[:1] + "***") if host else "***"
    return f"{local_bit}@{host_bit}.{tld or '***'}"


def _valid_email(email: str) -> bool:
    if not email or len(email) > 160:
        return False
    return bool(EMAIL_RE.match(email))


def _valid_password(password: str, username: str, email: str) -> str | None:
    if len(password) < 10:
        return "La contraseña tiene que tener al menos 10 caracteres"
    if len(password) > 128:
        return "La contraseña es demasiado larga"
    lowered = password.lower()
    if username and username.lower() in lowered:
        return "La contraseña no puede incluir el usuario"
    local = email.split("@", 1)[0]
    if local and len(local) >= 4 and local in lowered:
        return "La contraseña no puede incluir el mail"
    return None


def _dummy() -> str:
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = HASHER.hash("propmap-timing-dummy")
    return _dummy_hash


def _allow(bucket: str, limit: int, window: float) -> bool:
    now = time.monotonic()
    with _rate_lock:
        hits = [t for t in _rate.get(bucket, []) if now - t < window]
        if len(hits) >= limit:
            _rate[bucket] = hits
            return False
        hits.append(now)
        _rate[bucket] = hits
        return True


def _client_ip(request: Request | None) -> str:
    if request is None or request.client is None:
        return "local"
    return request.client.host or "local"


def _row_to_account(row) -> Account:
    username = _decrypt(row["username_enc"])
    email = _decrypt(row["email_enc"])
    return Account(
        id=row["id"],
        username=username,
        email_masked=mask_email(email),
        email_verified=bool(row["email_verified"]),
        created_at=row["created_at"] or "",
    )


def public_account(account: Account) -> dict[str, Any]:
    return {
        "username": account.username,
        "email_masked": account.email_masked,
        "email_verified": account.email_verified,
        "created_at": account.created_at,
    }


def _get_by_username(username: str):
    digest = _hmac(normalize_username(username).lower())
    with store.connect() as conn:
        return conn.execute("SELECT * FROM accounts WHERE username_hmac = ?", (digest,)).fetchone()


def _get_by_email(email: str):
    digest = _hmac(normalize_email(email))
    with store.connect() as conn:
        return conn.execute("SELECT * FROM accounts WHERE email_hmac = ?", (digest,)).fetchone()


def _get_by_id(user_id: str):
    with store.connect() as conn:
        return conn.execute("SELECT * FROM accounts WHERE id = ?", (user_id,)).fetchone()


def _signer() -> URLSafeTimedSerializer:
    _fernet, hmac_key = _keys()
    return URLSafeTimedSerializer(hmac_key.hex(), salt="propmap-verify")


def verify_url_for(user_id: str) -> str:
    token = _signer().dumps({"id": user_id})
    return f"{public_url()}/verificar?token={token}"


def _outbox_dir() -> Path:
    path = store.DATA_DIR / "mail"
    path.mkdir(parents=True, exist_ok=True)
    return path


def send_verify_email(email: str, username: str, link: str) -> None:
    subject = "Validá tu mail en PropMap"
    body = (
        f"Hola {username},\n\n"
        "Para validar esta cuenta de PropMap abrí este enlace (vence en 48 horas):\n"
        f"{link}\n\n"
        "Si no creaste esta cuenta, ignorá este mensaje. No guardamos tu mail en texto plano.\n"
    )
    from_addr = (os.environ.get("SMTP_FROM") or "PropMap <noreply@localhost>").strip()
    if smtp_configured():
        host = os.environ["SMTP_HOST"].strip()
        port = int(os.environ.get("SMTP_PORT") or 587)
        user = (os.environ.get("SMTP_USER") or "").strip()
        password = os.environ.get("SMTP_PASSWORD") or ""
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = email
        msg.set_content(body)
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            if (os.environ.get("SMTP_TLS") or "1").strip() not in {"0", "false", "no"}:
                smtp.starttls()
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)
        return
    digest = _hmac(email)[:16]
    (_outbox_dir() / f"{digest}.txt").write_text(
        f"To: {email}\nSubject: {subject}\n\n{body}",
        encoding="utf-8",
    )


def register(
    username: str,
    email: str,
    password: str,
    accept_terms: bool,
    request: Request | None = None,
) -> dict[str, Any]:
    store.init()
    ip = _client_ip(request)
    if not _allow(f"reg:{ip}", 8, 3600):
        raise HTTPException(429, "Demasiados intentos. Probá más tarde.")
    user = normalize_username(username)
    mail = normalize_email(email)
    if not USERNAME_RE.match(user):
        raise HTTPException(400, "El usuario va con letras, números o _ (3 a 32 caracteres)")
    if not _valid_email(mail):
        raise HTTPException(400, "El mail no es válido")
    if not accept_terms:
        raise HTTPException(400, "Tenés que aceptar los términos y la política de privacidad")
    problem = _valid_password(password, user, mail)
    if problem:
        raise HTTPException(400, problem)
    if _get_by_username(user) or _get_by_email(mail):
        raise HTTPException(409, "Ese usuario o mail ya está en uso")
    user_id = secrets.token_hex(16)
    now = _iso()
    with store._write:
        with store.connect() as conn:
            conn.execute(
                """
                INSERT INTO accounts(
                    id, username_hmac, username_enc, email_hmac, email_enc,
                    password_hash, email_verified, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    user_id,
                    _hmac(user.lower()),
                    _encrypt(user),
                    _hmac(mail),
                    _encrypt(mail),
                    HASHER.hash(password),
                    now,
                ),
            )
            conn.commit()
    link = verify_url_for(user_id)
    try:
        send_verify_email(mail, user, link)
    except Exception:
        pass
    payload: dict[str, Any] = {
        "ok": True,
        "user": public_account(
            Account(id=user_id, username=user, email_masked=mask_email(mail), email_verified=False, created_at=now)
        ),
        "message": "Te mandamos un mail para validar la cuenta.",
    }
    if reveal_verify_link():
        payload["verify_url"] = link
    return payload


def login(username: str, password: str, request: Request | None = None) -> tuple[Account, str]:
    store.init()
    ip = _client_ip(request)
    if not _allow(f"login:{ip}", 12, 900):
        raise HTTPException(429, "Demasiados intentos. Probá más tarde.")
    raw = (username or "").strip()
    row = _get_by_username(raw) if raw else None
    if row is None and "@" in raw:
        row = _get_by_email(normalize_email(raw))
    hashed = row["password_hash"] if row else _dummy()
    try:
        HASHER.verify(hashed, password or "")
        ok = bool(row)
    except (VerifyMismatchError, InvalidHash):
        ok = False
    if not ok:
        raise HTTPException(401, "Usuario o contraseña incorrectos")
    if HASHER.check_needs_rehash(row["password_hash"]):
        with store._write:
            with store.connect() as conn:
                conn.execute(
                    "UPDATE accounts SET password_hash = ? WHERE id = ?",
                    (HASHER.hash(password), row["id"]),
                )
                conn.commit()
    token = secrets.token_urlsafe(32)
    expires = _now() + timedelta(days=SESSION_DAYS)
    with store._write:
        with store.connect() as conn:
            conn.execute(
                "INSERT INTO account_sessions(token_hmac, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
                (_hmac(token), row["id"], _iso(expires), _iso()),
            )
            conn.commit()
    return _row_to_account(row), token


def stamp_session(response: Response, token: str, request: Request | None = None) -> None:
    secure = bool(request and request.url.scheme == "https")
    response.set_cookie(
        COOKIE,
        token,
        max_age=SESSION_DAYS * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(COOKIE, path="/")


def logout(request: Request, response: Response) -> None:
    token = request.cookies.get(COOKIE) or ""
    if token:
        with store._write:
            with store.connect() as conn:
                conn.execute("DELETE FROM account_sessions WHERE token_hmac = ?", (_hmac(token),))
                conn.commit()
    clear_session(response)


def optional_user(request: Request | None) -> Account | None:
    if request is None:
        return None
    token = request.cookies.get(COOKIE) or ""
    if not token:
        return None
    store.init()
    digest = _hmac(token)
    now = _iso()
    with store.connect() as conn:
        row = conn.execute(
            "SELECT s.user_id, s.expires_at, a.* FROM account_sessions s JOIN accounts a ON a.id = s.user_id WHERE s.token_hmac = ?",
            (digest,),
        ).fetchone()
    if not row or str(row["expires_at"] or "") < now:
        return None
    return _row_to_account(row)


def require_user(request: Request, verified: bool = False) -> Account:
    user = optional_user(request)
    if not user:
        raise HTTPException(401, "Entrá con tu cuenta")
    if verified and not user.email_verified:
        raise HTTPException(403, "Validá tu mail para guardar datos en la cuenta")
    return user


def verify_token(token: str) -> Account:
    store.init()
    try:
        data = _signer().loads(token, max_age=VERIFY_MAX_AGE)
    except SignatureExpired as exc:
        raise HTTPException(400, "Ese enlace venció. Pedí otro desde Ajustes.") from exc
    except BadSignature as exc:
        raise HTTPException(400, "El enlace no es válido") from exc
    user_id = str((data or {}).get("id") or "")
    row = _get_by_id(user_id)
    if not row:
        raise HTTPException(400, "Esa cuenta ya no existe")
    with store._write:
        with store.connect() as conn:
            conn.execute(
                "UPDATE accounts SET email_verified = 1, verified_at = ? WHERE id = ?",
                (_iso(), user_id),
            )
            conn.commit()
    row = _get_by_id(user_id)
    return _row_to_account(row)


def resend_verification(user: Account) -> dict[str, Any]:
    if user.email_verified:
        return {"ok": True, "message": "El mail ya está validado."}
    if not _allow(f"verify:{user.id}", 4, 3600):
        raise HTTPException(429, "Esperá un rato para pedir otro mail")
    row = _get_by_id(user.id)
    if not row:
        raise HTTPException(404, "Esa cuenta ya no existe")
    email = _decrypt(row["email_enc"])
    link = verify_url_for(user.id)
    try:
        send_verify_email(email, user.username, link)
    except Exception:
        pass
    payload: dict[str, Any] = {"ok": True, "message": "Te mandamos de nuevo el mail de validación."}
    if reveal_verify_link():
        payload["verify_url"] = link
    return payload


def _vault_of(user_id: str) -> dict[str, Any]:
    with store.connect() as conn:
        row = conn.execute("SELECT cipher FROM account_vaults WHERE user_id = ?", (user_id,)).fetchone()
    if not row:
        return {"pins": {}}
    try:
        data = json.loads(_decrypt(row["cipher"]))
    except Exception:
        return {"pins": {}}
    if not isinstance(data, dict):
        return {"pins": {}}
    pins = data.get("pins")
    if not isinstance(pins, dict):
        data["pins"] = {}
    return data


def _save_vault(user_id: str, vault: dict[str, Any]) -> None:
    blob = _encrypt(json.dumps(vault, ensure_ascii=False, separators=(",", ":")))
    with store._write:
        with store.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO account_vaults(user_id, cipher) VALUES (?, ?)",
                (user_id, blob),
            )
            conn.commit()


def overlay_pins(listings: list[dict], user: Account | None) -> list[dict]:
    pins = (_vault_of(user.id).get("pins") or {}) if user else {}
    if not user or not pins:
        return listings
    out = []
    for row in listings:
        item = dict(row)
        item["favorite"] = False
        item["notes"] = ""
        item["contacted"] = False
        pin = pins.get(item.get("id"))
        if isinstance(pin, dict):
            item["favorite"] = bool(pin.get("favorite"))
            item["notes"] = str(pin.get("notes") or "")[:2000]
            item["contacted"] = bool(pin.get("contacted"))
        out.append(item)
    return out


def save_pin(
    user: Account,
    listing_id: str,
    favorite: bool | None = None,
    notes: str | None = None,
    contacted: bool | None = None,
) -> dict[str, Any]:
    listing_id = str(listing_id or "").strip()
    if not listing_id or len(listing_id) > 160:
        raise HTTPException(400, "Aviso inválido")
    vault = _vault_of(user.id)
    pins = dict(vault.get("pins") or {})
    current = dict(pins.get(listing_id) or {})
    if favorite is not None:
        current["favorite"] = bool(favorite)
    if notes is not None:
        current["notes"] = str(notes)[:2000]
    if contacted is not None:
        current["contacted"] = bool(contacted)
    current.setdefault("favorite", False)
    current.setdefault("notes", "")
    current.setdefault("contacted", False)
    if current["favorite"] or current["notes"] or current["contacted"]:
        pins[listing_id] = current
    else:
        pins.pop(listing_id, None)
    vault["pins"] = pins
    _save_vault(user.id, vault)
    return {"id": listing_id, **current}


def import_pins(user: Account, pins_in: dict[str, Any]) -> dict[str, Any]:
    vault = _vault_of(user.id)
    pins = dict(vault.get("pins") or {})
    for listing_id, raw in (pins_in or {}).items():
        key = str(listing_id)[:160]
        if not key or not isinstance(raw, dict):
            continue
        current = dict(pins.get(key) or {})
        if "favorite" in raw:
            current["favorite"] = bool(raw.get("favorite"))
        if "notes" in raw:
            current["notes"] = str(raw.get("notes") or "")[:2000]
        if "contacted" in raw:
            current["contacted"] = bool(raw.get("contacted"))
        current.setdefault("favorite", False)
        current.setdefault("notes", "")
        current.setdefault("contacted", False)
        pins[key] = current
    vault["pins"] = pins
    _save_vault(user.id, vault)
    return {"ok": True, "count": len(pins)}


def delete_account(user: Account, password: str, confirm: str) -> None:
    if (confirm or "").strip().upper() not in {user.username.upper(), "ELIMINAR"}:
        raise HTTPException(400, "Escribí tu usuario o ELIMINAR para confirmar")
    row = _get_by_id(user.id)
    if not row:
        raise HTTPException(404, "Esa cuenta ya no existe")
    try:
        HASHER.verify(row["password_hash"], password or "")
    except (VerifyMismatchError, InvalidHash) as exc:
        raise HTTPException(401, "Contraseña incorrecta") from exc
    with store._write:
        with store.connect() as conn:
            conn.execute("DELETE FROM account_sessions WHERE user_id = ?", (user.id,))
            conn.execute("DELETE FROM account_vaults WHERE user_id = ?", (user.id,))
            conn.execute("DELETE FROM accounts WHERE id = ?", (user.id,))
            conn.commit()
