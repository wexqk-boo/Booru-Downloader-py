"""Lightweight CLI configuration: board credentials from .env, default paths.

Self-contained: the .env parser and provider credentials live here.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .providers import PROVIDERS

DEFAULT_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

#: Name used under every XDG directory (config / data / cache / state).
APP_DIR = "bdl"

#: Parallel media transfers when neither config.json nor a flag says otherwise.
#: 8 keeps a 100 Mbit line busy without opening a socket storm.
DEFAULT_CONCURRENCY = 8

#: ``--turbo`` and the ceiling the adaptive ramp may climb to: for the fat
#: pipes (1-5 Gbit/s) where a small pool leaves the line idle.
TURBO_CONCURRENCY = 20

#: Hard cap, so a typo in config.json or a flag cannot open 10 000 sockets.
MAX_CONCURRENCY = 64


@dataclass(frozen=True, slots=True)
class ProviderCredentials:
    api_key: str = ""
    user_id: str = ""
    login: str = ""

    def for_url(self) -> dict[str, str]:
        data = {"api_key": self.api_key.strip()}
        if self.user_id.strip():
            data["user_id"] = self.user_id.strip()
        if self.login.strip():
            data["login"] = self.login.strip()
        return {key: value for key, value in data.items() if value}


def _parse_value(raw: str) -> str:
    """Handle VALUE, "quoted" and 'quoted', stripping inline comments."""
    raw = raw.strip()
    if raw[:1] in {'"', "'"}:
        quote = raw[0]
        end = raw.find(quote, 1)
        return raw[1:end] if end != -1 else raw[1:]
    return raw.split(" #", 1)[0].split("\t#", 1)[0].strip()


def load_dotenv(path: Path) -> dict[str, str]:
    """Minimal .env parser: KEY=VALUE, # comments, quotes, export."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError:
        return values

    for raw in raw_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        values[key] = _parse_value(value)
    return values


def _env_or_dotenv(name: str, dotenv: dict[str, str]) -> str:
    value = os.environ.get(name)
    return (value or dotenv.get(name, "")).strip()


def load_credentials(env_file: str | Path | None = None) -> dict[str, ProviderCredentials]:
    """Read .env (or the real environment) and collect credentials per provider."""
    path = Path(env_file).expanduser() if env_file else DEFAULT_ENV_FILE
    dotenv = load_dotenv(path)
    credentials: dict[str, ProviderCredentials] = {}
    for key in PROVIDERS:
        prefix = key.upper()
        credentials[key] = ProviderCredentials(
            api_key=_env_or_dotenv(f"{prefix}_API_KEY", dotenv),
            user_id=_env_or_dotenv(f"{prefix}_USER_ID", dotenv),
            login=_env_or_dotenv(f"{prefix}_LOGIN", dotenv),
        )
    return credentials


def load_proxy(env_file: str | Path | None = None) -> str:
    path = Path(env_file).expanduser() if env_file else DEFAULT_ENV_FILE
    dotenv = load_dotenv(path)
    return _env_or_dotenv("PROXY_URL", dotenv) or _env_or_dotenv("HTTPS_PROXY", dotenv)


def xdg_config_home() -> Path:
    return Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config"))


def xdg_data_home() -> Path:
    return Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local" / "share"))


def xdg_cache_home() -> Path:
    return Path(os.getenv("XDG_CACHE_HOME", Path.home() / ".cache"))


def xdg_state_home() -> Path:
    return Path(os.getenv("XDG_STATE_HOME", Path.home() / ".local" / "state"))


def default_db_path() -> Path:
    """SQLite download state: ~/.local/share/bdl/downloads.sqlite3."""
    return xdg_data_home() / APP_DIR / "downloads.sqlite3"


def env_first(*names: str) -> str:
    """First non-empty environment variable among ``names``."""
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def config_path() -> Path:
    """~/.config/bdl/config.json — CLI settings."""
    return xdg_config_home() / APP_DIR / "config.json"


def load_config() -> dict[str, str]:
    path = config_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(values: dict[str, str]) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp file so a crash never truncates the live config.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def get_setting(name: str) -> str:
    return load_config().get(name, "")


def set_setting(name: str, value: str) -> None:
    values = load_config()
    values[name] = value
    save_config(values)


#: Settings ``bdl config set`` accepts, with a validator and a short help line.
#: Anything else is stored verbatim, which keeps the command useful for
#: settings added later without a code change.
_CONFIG_VALIDATORS: dict[str, Callable[[str], str | None]] = {}


def _concurrency_validator(raw: str) -> str | None:
    value = parse_concurrency(raw)
    if value is None:
        return f"concurrency must be a whole number between 1 and {MAX_CONCURRENCY}"
    return None


_CONFIG_VALIDATORS["concurrency"] = _concurrency_validator


def config_validator(key: str) -> Callable[[str], str | None] | None:
    """Return the checker for a config key, or None when it needs no checking."""
    return _CONFIG_VALIDATORS.get(key)


def default_out_dir() -> Path:
    """Default root folder: BDL_OUT, then config.json ``out``, then ~/Pictures/booru.

    Every command that takes ``--out`` falls back to this, so both the
    environment and ``bdl config set out ...`` behave as expected.
    """
    raw = env_first("BDL_OUT", "BOORU_DL_OUT") or get_setting("out").strip()
    return Path(raw).expanduser() if raw else Path.home() / "Pictures" / "booru"


def clamp_concurrency(value: int) -> int:
    """Keep a pool size inside sane bounds, whatever the source."""
    return max(1, min(MAX_CONCURRENCY, value))


def parse_concurrency(raw: str) -> int | None:
    """Read a concurrency setting, or None when it is absent or unusable.

    A bad value in config.json must not stop a download: the caller falls back
    to the default and the user finds out through ``bdl config get``.
    """
    text = raw.strip()
    if not text:
        return None
    try:
        return clamp_concurrency(int(text))
    except ValueError:
        return None


def default_concurrency() -> int:
    """Pool size to start a run with: BDL_CONCURRENCY, config.json, then 8.

    The environment wins because it is the per-invocation escape hatch, and the
    saved setting is what ``bdl config set concurrency N`` writes.
    """
    from_env = parse_concurrency(env_first("BDL_CONCURRENCY"))
    if from_env is not None:
        return from_env
    from_config = parse_concurrency(get_setting("concurrency"))
    return from_config if from_config is not None else DEFAULT_CONCURRENCY


@dataclass(frozen=True, slots=True)
class ClientSettingsShim:
    """Duck-typed settings for ``ProviderClient`` (``bdl.providers.client``).

    ``ProviderClient`` only reads ``cache_ttl``, ``max_cache_entries``,
    ``proxy`` and ``credentials_for()``, so the full settings object is overkill here.
    """

    credentials: dict[str, ProviderCredentials]
    cache_ttl: float = 180.0
    max_cache_entries: int = 256
    proxy: str = ""

    def credentials_for(self, provider_key: str) -> dict[str, str]:
        creds = self.credentials.get(provider_key)
        return creds.for_url() if creds else {}
