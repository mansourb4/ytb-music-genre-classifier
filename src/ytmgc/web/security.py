"""Contrôle d'accès de l'interface web.

L'application pilote un compte YouTube Music : quiconque atteint son port peut
réécrire une bibliothèque ou supprimer des playlists. Tant qu'elle n'écoute que
sur la boucle locale, l'isolement suffit. Dès qu'elle est joignable de
l'extérieur — réseau local, port transféré d'un Codespace — un jeton devient
obligatoire.

Ce jeton ne remplace pas l'authentification de l'hébergeur (celle de GitHub sur
un port transféré privé, par exemple) : il s'y ajoute, et couvre le cas où ce
port passerait en visibilité publique.
"""

from __future__ import annotations

import secrets
from typing import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

COOKIE_NAME = "ytmgc_token"
HEADER_NAME = "x-ytmgc-token"
QUERY_NAME = "token"


def generate_token() -> str:
    return secrets.token_urlsafe(24)


def _presented(request: Request) -> str | None:
    """Jeton fourni par le client, quelle que soit sa provenance.

    L'ordre importe peu, mais le paramètre d'URL vient en premier : c'est lui
    qui permet d'ouvrir le lien sur un téléphone, sans rien saisir.
    """
    return (
        request.query_params.get(QUERY_NAME)
        or request.headers.get(HEADER_NAME)
        or request.cookies.get(COOKIE_NAME)
    )


def install_token_guard(app: FastAPI, token: str) -> None:
    """Exige `token` sur toutes les requêtes servies par `app`."""

    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        presented = _presented(request)
        # compare_digest plutôt que == : la comparaison ne doit pas fuir la
        # longueur du préfixe correct par son temps d'exécution.
        if presented is None or not secrets.compare_digest(presented, token):
            return JSONResponse(
                {"detail": "Jeton d'accès requis ou invalide."}, status_code=401
            )

        response = await call_next(request)

        if request.query_params.get(QUERY_NAME):
            # Le lien n'a plus à porter le jeton ensuite : le navigateur le
            # renverra seul, y compris pour les appels de l'interface.
            response.set_cookie(
                COOKIE_NAME,
                token,
                httponly=True,
                samesite="lax",
                secure=request.url.scheme == "https",
                max_age=30 * 24 * 3600,
            )
        return response
