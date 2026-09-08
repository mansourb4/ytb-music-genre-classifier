"""Chargement de la configuration (config/config.toml + variables d'environnement)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path("config/config.toml")


@dataclass(slots=True)
class StoreConfig:
    path: str = "data/ytmgc.db"


@dataclass(slots=True)
class YouTubeConfig:
    auth_file: str = "browser.json"
    #: Identifiant client OAuth (connexion depuis un appareil sans navigateur).
    #: Secret local, jamais versionné ; peut aussi venir de l'environnement.
    oauth_client_file: str = "oauth_client.json"
    sources: list[str] = field(default_factory=lambda: ["library", "liked"])
    playlist_privacy: str = "PRIVATE"


@dataclass(slots=True)
class DiscogsConfig:
    user_agent: str = "ytb-music-genre-classifier/0.1"
    rate_limit_per_minute: int = 50
    cache_ttl_days: int = 90
    #: Jamais lu depuis le fichier : uniquement depuis l'environnement.
    token: str = ""


@dataclass(slots=True)
class MatchingConfig:
    min_score: float = 0.72
    review_score: float = 0.55
    max_candidates: int = 10


@dataclass(slots=True)
class TaxonomyConfig:
    multi_style: str = "all"
    max_styles_per_track: int = 2
    min_tracks_per_style: int = 4
    min_tracks_per_genre: int = 3
    fallback_playlist: str = "Divers — genres isolés"
    style_name_template: str = "{genre} — {style}"
    genre_name_template: str = "{genre} — Autres styles"


@dataclass(slots=True)
class WebConfig:
    """Interface web locale.

    `access_token` ne vient jamais du fichier de configuration : c'est un
    secret, il est lu dans l'environnement (`YTMGC_ACCESS_TOKEN`) ou engendré
    au démarrage.
    """

    host: str = "127.0.0.1"
    port: int = 8765
    #: Exiger le jeton dès que l'écoute dépasse la boucle locale. Le désactiver
    #: rend l'interface librement utilisable par quiconque atteint le port.
    require_token: bool = True
    access_token: str = ""

    def is_loopback(self) -> bool:
        return self.host in {"127.0.0.1", "localhost", "::1"}

    def token_required(self) -> bool:
        """Un jeton n'est imposé que si l'interface est joignable de l'extérieur."""
        return self.require_token and not self.is_loopback()


@dataclass(slots=True)
class SyncConfig:
    marker: str = "[ytmgc]"
    prune: bool = True
    batch_size: int = 50


@dataclass(slots=True)
class Config:
    store: StoreConfig = field(default_factory=StoreConfig)
    youtube: YouTubeConfig = field(default_factory=YouTubeConfig)
    discogs: DiscogsConfig = field(default_factory=DiscogsConfig)
    matching: MatchingConfig = field(default_factory=MatchingConfig)
    taxonomy: TaxonomyConfig = field(default_factory=TaxonomyConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    web: WebConfig = field(default_factory=WebConfig)

    def validate(self) -> None:
        if self.taxonomy.multi_style not in {"primary", "all"}:
            raise ValueError("taxonomy.multi_style doit valoir 'primary' ou 'all'")
        if not 0 < self.matching.min_score <= 1:
            raise ValueError("matching.min_score doit être dans ]0, 1]")
        if self.matching.review_score > self.matching.min_score:
            raise ValueError("matching.review_score doit être <= matching.min_score")
        if self.youtube.playlist_privacy not in {"PRIVATE", "UNLISTED", "PUBLIC"}:
            raise ValueError("youtube.playlist_privacy doit valoir PRIVATE, UNLISTED ou PUBLIC")
        if not 1 <= self.web.port <= 65535:
            raise ValueError("web.port doit être un port valide")
        if not self.sync.marker:
            raise ValueError("sync.marker ne peut pas être vide : il protège les playlists manuelles")


def _apply_section(section: Any, values: dict[str, Any], path: str) -> None:
    known = {f.name for f in fields(section)}
    for key, value in values.items():
        if key not in known:
            raise ValueError(f"Clé de configuration inconnue : {path}.{key}")
        setattr(section, key, value)


def load_config(path: Path | None = None, env: dict[str, str] | None = None) -> Config:
    """Construit la configuration : valeurs par défaut < fichier TOML < environnement.

    Les secrets (jeton Discogs) ne viennent *que* de l'environnement, pour ne
    jamais finir versionnés dans config.toml.
    """
    env = os.environ if env is None else env
    config = Config()

    config_path = path or DEFAULT_CONFIG_PATH
    if config_path.exists():
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        for name, values in data.items():
            section = getattr(config, name, None)
            if section is None or not is_dataclass(section):
                raise ValueError(f"Section de configuration inconnue : [{name}]")
            _apply_section(section, values, name)

    config.discogs.token = env.get("DISCOGS_TOKEN", "")
    config.web.access_token = env.get("YTMGC_ACCESS_TOKEN", "")
    if user_agent := env.get("DISCOGS_USER_AGENT"):
        config.discogs.user_agent = user_agent
    if auth_file := env.get("YTMUSIC_AUTH_FILE"):
        config.youtube.auth_file = auth_file

    config.validate()
    return config
