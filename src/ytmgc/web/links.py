"""Construction de l'URL à ouvrir.

Le lien affiché au démarrage doit être utilisable tel quel — y compris collé
dans un téléphone. Une adresse d'écoute n'est pas une adresse de visite :
`0.0.0.0` signifie « toutes les interfaces » et n'est pas joignable, et dans un
Codespace l'interface se rejoint par le domaine de port transféré, pas par le
port local.
"""

from __future__ import annotations

import os

#: Renseignées par Codespaces dans l'environnement du conteneur.
CODESPACE_NAME = "CODESPACE_NAME"
CODESPACE_DOMAIN = "GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN"


def access_url(host: str, port: int, token: str = "", env: dict[str, str] | None = None) -> str:
    """URL à ouvrir dans un navigateur, jeton compris s'il y en a un."""
    env = os.environ if env is None else env

    name, domain = env.get(CODESPACE_NAME), env.get(CODESPACE_DOMAIN)
    if name and domain:
        base = f"https://{name}-{port}.{domain}"
    else:
        # "0.0.0.0" et "::" désignent l'écoute, pas une destination joignable.
        visitable = "127.0.0.1" if host in {"0.0.0.0", "::", ""} else host
        base = f"http://{visitable}:{port}"

    return f"{base}/?token={token}" if token else base


def in_codespace(env: dict[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return bool(env.get(CODESPACE_NAME) and env.get(CODESPACE_DOMAIN))
