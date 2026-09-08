"""Connexion OAuth par code d'appareil.

La connexion par en-têtes de navigateur suppose des outils de développement,
donc un ordinateur. Google propose pour les appareils sans clavier ni
navigateur un flux dit « limited input » : l'application affiche une URL courte
et un code, l'utilisateur les saisit sur n'importe quel appareil, et
l'application récupère un jeton en interrogeant Google.

C'est la seule voie de connexion praticable depuis un téléphone seul.

Contrepartie : Google exige un identifiant client OAuth propre à chaque
application, que l'utilisateur crée dans la console Google Cloud (type
« Téléviseurs et périphériques à saisie limitée »). Cet identifiant reste un
secret local, jamais versionné.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Réponses d'erreur de Google signifiant « l'utilisateur n'a pas encore validé ».
PENDING = {"authorization_pending", "slow_down"}
#: Réponses définitives : il faut relancer le flux.
FATAL = {
    "expired_token": "Le code a expiré avant la validation. Relance la connexion.",
    "access_denied": "La demande a été refusée dans la fenêtre Google.",
    "invalid_client": "Identifiant client refusé par Google : vérifie l'ID et le secret.",
    "unauthorized_client": "Ce client OAuth n'est pas du type « saisie limitée ».",
}


@dataclass(frozen=True, slots=True)
class OAuthClient:
    client_id: str
    client_secret: str

    def credentials(self) -> Any:
        from ytmusicapi import OAuthCredentials  # import différé

        return OAuthCredentials(client_id=self.client_id, client_secret=self.client_secret)


def load_client(path: str | Path, env: dict[str, str] | None = None) -> OAuthClient | None:
    """Identifiant client, depuis l'environnement puis depuis le fichier local."""
    import os

    env = os.environ if env is None else env
    client_id = env.get("YTMGC_OAUTH_CLIENT_ID", "")
    client_secret = env.get("YTMGC_OAUTH_CLIENT_SECRET", "")
    if client_id and client_secret:
        return OAuthClient(client_id, client_secret)

    file = Path(path)
    if not file.exists():
        return None
    data = json.loads(file.read_text(encoding="utf-8"))
    if data.get("client_id") and data.get("client_secret"):
        return OAuthClient(data["client_id"], data["client_secret"])
    return None


def save_client(path: str | Path, client: OAuthClient) -> None:
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(
        json.dumps({"client_id": client.client_id, "client_secret": client.client_secret}, indent=1),
        encoding="utf-8",
    )
    # Un secret n'a pas à être lisible par les autres comptes de la machine.
    file.chmod(0o600)


def interpret(raw: dict[str, Any]) -> tuple[str, str]:
    """Traduit la réponse de Google en (état, message).

    États : « en attente », « connecté », « échec ». Isolé du réseau pour être
    testable — c'est la partie où les erreurs coûtent cher.
    """
    error = raw.get("error")
    if error in PENDING:
        return "en attente", "En attente de ta validation dans la fenêtre Google…"
    if error:
        return "échec", FATAL.get(error, f"Google a refusé la demande : {error}")
    if raw.get("access_token") and raw.get("refresh_token"):
        return "connecté", "Compte connecté."
    return "échec", "Réponse inattendue de Google."


def start_device_flow(client: OAuthClient, credentials: Any | None = None) -> dict[str, Any]:
    """Demande un code d'appareil. Renvoie l'URL et le code à saisir."""
    code = (credentials or client.credentials()).get_code()
    return {
        "device_code": code["device_code"],
        "user_code": code["user_code"],
        "verification_url": code["verification_url"],
        # Lien direct : le code y est déjà pré-rempli, ce qui évite une saisie
        # manuelle sur un téléphone.
        "url": f"{code['verification_url']}?user_code={code['user_code']}",
        "interval": code.get("interval", 5),
        "expires_in": code.get("expires_in", 1800),
    }


def complete_device_flow(
    client: OAuthClient,
    device_code: str,
    token_path: str | Path,
    credentials: Any | None = None,
) -> tuple[str, str]:
    """Interroge Google une fois. Écrit le jeton si la validation a eu lieu."""
    creds = credentials or client.credentials()
    # Un seul appel : le code d'appareil est à usage unique, le rejouer échouerait.
    raw = dict(creds.token_from_code(device_code))
    state, message = interpret(raw)
    if state == "connecté":
        _store_token(creds, raw, token_path)
    return state, message


def _store_token(credentials: Any, raw: dict[str, Any], token_path: str | Path) -> None:
    """Écrit le jeton au format attendu par ytmusicapi.

    On délègue à `RefreshingToken` plutôt que de composer le JSON à la main :
    c'est lui qui calcule `expires_at` et qui définit le format lu au démarrage.
    """
    from ytmusicapi.auth.oauth import RefreshingToken

    Path(token_path).parent.mkdir(parents=True, exist_ok=True)
    token = RefreshingToken(
        credentials=credentials,
        access_token=raw["access_token"],
        refresh_token=raw["refresh_token"],
        scope=raw["scope"],
        token_type=raw["token_type"],
        expires_in=raw.get("refresh_token_expires_in", raw["expires_in"]),
    )
    token.update(raw)
    token.store_token(str(token_path))
    Path(token_path).chmod(0o600)
