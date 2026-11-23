"""Hashing de passwords con bcrypt (sal aleatoria por hash).

Sin mock: roundtrip real (hash -> verify). bcrypt 5.x.
"""
from __future__ import annotations

import bcrypt


def hash_password(plain: str) -> str:
    """Hashea una password en claro -> string bcrypt listo para almacenar."""
    if not plain:
        raise ValueError("La password no puede estar vacia")
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """True si plain corresponde al hash bcrypt. False ante cualquier error
    (password vacia, hash malformado, no coincide)."""
    if not plain or not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False
