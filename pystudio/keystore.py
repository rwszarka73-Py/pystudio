"""Secrets (the Claude API key, remembered database passwords) in the desktop's keyring - on Ubuntu that's
GNOME Keyring, the same place Wi-Fi passwords are kept, unlocked when you log in. Seahorse ("Passwords and
Keys") shows them under "PyStudio".

If there's no keyring (no `keyring` module, or no Secret Service running), PyStudio falls back to its
settings file, like before. Values saved there by older versions are moved into the keyring automatically."""
from PySide6.QtCore import QSettings

SERVICE = "PyStudio"
_backend = "unknown"      # "keyring" | "settings"


def _keyring():
    """The keyring module if a real Secret Service backend is available, else None."""
    global _backend
    if _backend == "settings":
        return None
    try:
        import keyring
        from keyring.backends import fail
        kr = keyring.get_keyring()
        names = type(kr).__module__ + "." + type(kr).__name__
        if isinstance(kr, fail.Keyring) or "null" in names.lower():
            raise RuntimeError("no keyring backend")
        if "chainer" in names.lower() and not getattr(kr, "backends", None):
            raise RuntimeError("no keyring backend")
        _backend = "keyring"
        return keyring
    except Exception:  # noqa: BLE001 - any problem: use the settings file
        _backend = "settings"
        return None


def where():
    """Where secrets are kept, for messages: 'your desktop keyring' or the settings file."""
    return "your desktop keyring (Passwords and Keys)" if _keyring() else "PyStudio's settings file"


def get(key, default=""):
    kr = _keyring()
    s = QSettings()
    if kr is not None:
        try:
            v = kr.get_password(SERVICE, key)
            if v is not None:
                return v
            old = s.value(key, "")              # saved by an older PyStudio: move it into the keyring
            if old:
                kr.set_password(SERVICE, key, str(old))
                s.remove(key)
                return str(old)
            return default
        except Exception:  # noqa: BLE001 - keyring locked/unavailable now
            pass
    return s.value(key, default) or default


def set(key, value):  # noqa: A001 - mirrors keyring's API
    kr = _keyring()
    s = QSettings()
    if kr is not None:
        try:
            if value:
                kr.set_password(SERVICE, key, value)
            else:
                try:
                    kr.delete_password(SERVICE, key)
                except Exception:  # noqa: BLE001 - wasn't there
                    pass
            s.remove(key)                       # never leave a copy in the settings file
            return
        except Exception:  # noqa: BLE001
            pass
    if value:
        s.setValue(key, value)
    else:
        s.remove(key)


def delete(key):
    set(key, "")
