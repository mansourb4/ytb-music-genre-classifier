# ytb-music-genre-classifier

Scanne l'intégralité d'une bibliothèque **YouTube Music** et l'organise **par
genre et par style**, en s'appuyant sur la taxonomie de **Discogs**.

## Pourquoi des playlists et pas des dossiers

YouTube Music ne connaît **que des playlists** : il n'existe aucun dossier, ni
dans l'interface, ni dans l'API officielle, ni dans l'API interne. La hiérarchie
`genre → style` de Discogs est donc encodée dans le **nom** de la playlist, de
sorte que le tri alphabétique de la bibliothèque regroupe les styles d'un même
genre :

```
Electronic — Deep House
Electronic — Drum & Bass
Electronic — Jungle
Electronic — Techno
Rock — Grunge
Rock — Post-Punk
Rock — Shoegaze
```

## Comprendre, pas seulement ranger

Le nommage est volontairement fin : Jungle n'est pas rangé dans Drum & Bass,
Indie Rock n'est pas rangé dans Indie. Les alias de styles ne servent qu'à
réunir des **variantes d'écriture** d'un même style ; fusionner deux styles
réellement distincts effacerait l'information recherchée.

Chaque playlist porte en description la définition de son genre et de son
style :

```
[ytmgc] key=electronic/deep-house

Electronic — Deep House · 34 titre(s)

GENRE — Electronic
Musiques dont le son est produit ou transformé par des moyens électroniques —
synthétiseurs, boîtes à rythmes, échantillonneurs, ordinateurs. Recouvre aussi
bien la musique de club que l'écoute domestique et l'expérimentation.

STYLE — Deep House
Branche la plus soul de la house : tempo modéré, accords de septième et de
neuvième empruntés au jazz, nappes profondes et voix feutrées.

Playlist générée automatiquement à partir de la taxonomie Discogs.
Les modifications manuelles seront écrasées au prochain run.
```

Ces définitions vivent dans `src/ytmgc/taxonomy/descriptions.toml` : 15 genres
et près de 200 styles. Un style non encore décrit reste parfaitement utilisable,
sa playlist est simplement créée sans définition — Discogs en ajoute
régulièrement.

## Installation

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

### Connecter le compte YouTube Music

YouTube Music n'a pas d'API publique, et l'API officielle YouTube Data v3 ne
convient pas ici : elle n'expose pas la bibliothèque YouTube Music, et son
quota (10 000 unités/jour, 50 par insertion) plafonne à environ **200 titres
ajoutés par jour**. Il n'existe donc pas de bouton « Se connecter avec Google »
au sens habituel. L'interface propose quatre voies, de la plus simple à la plus
manuelle.

**1. Ma session de navigateur** — si tu es déjà connecté à YouTube Music dans
Firefox ou Chrome, l'application reprend cette session. Aucune saisie.
Fiable sur Firefox, macOS et Linux ; sous Windows, Chrome chiffre ses cookies
depuis la version 127, préfère Firefox.

**2. Fenêtre de connexion** — l'application ouvre une fenêtre sur YouTube
Music, tu t'y connectes normalement, elle relève la session et referme. Google
refuse parfois l'authentification dans un navigateur piloté ; le profil est
persistant, donc une session déjà validée est réutilisée.

Ces deux méthodes demandent une dépendance supplémentaire :

```bash
pip install -e ".[browser]"
playwright install chromium     # uniquement pour la fenêtre de connexion
```

**3. Compte Google (OAuth)** — connexion par code d'appareil, indépendante de
tout navigateur local. Il faut créer une fois un identifiant client dans la
console Google Cloud (projet → activer *YouTube Data API v3* → identifiants →
ID client OAuth de type **Téléviseurs et périphériques à saisie limitée**), car
Google impose à chaque application de s'enregistrer. L'identifiant peut venir
de `YTMGC_OAUTH_CLIENT_ID` / `YTMGC_OAUTH_CLIENT_SECRET`.

**4. En-têtes (avancé)** — à réserver aux cas où les autres échouent. Le geste
est le même dans tous les navigateurs :

1. ouvrir `music.youtube.com` connecté, puis `F12` → onglet **Réseau** /
   **Network** ;
2. panneau ouvert, **cliquer sur « Bibliothèque » dans la page** : la liste des
   requêtes se remplit (l'inspecteur ne montre que ce qui suit son ouverture) ;
3. **clic droit sur n'importe quelle ligne** → *Copier* → **« Copier comme
   cURL »** (*Copy as cURL (bash)* dans Chrome et Edge ; éviter *Copy as
   PowerShell*) ;
4. coller dans l'interface.

L'application n'extrait que le cookie de session et reconstruit le reste des
en-têtes ; n'importe quelle requête authentifiée du domaine convient donc. Une
liste d'en-têtes bruts reste acceptée. En ligne de commande, l'équivalent est :

```bash
ytmusicapi browser   # crée browser.json en suivant les instructions affichées
```

### Jeton Discogs

Crée un jeton personnel sur <https://www.discogs.com/settings/developers>, puis :

```bash
cp .env.example .env   # renseigne DISCOGS_TOKEN
export $(grep -v '^#' .env | xargs)
```

Le jeton n'est **jamais** lu depuis `config.toml` : uniquement depuis
l'environnement, pour qu'il ne finisse pas versionné.

### Configuration

```bash
cp config/config.example.toml config/config.toml
```

Toutes les options sont commentées dans le fichier d'exemple : sources scannées,
seuils d'appariement, seuils de regroupement, politique multi-styles.

## Utilisation — interface web

```bash
pip install -e ".[web]"
ytmgc web            # http://127.0.0.1:8765
```

### Depuis un téléphone, via GitHub Codespaces

Le dépôt contient un `.devcontainer/` prêt à l'emploi :

1. Sur GitHub : **Code → Codespaces → Create codespace**. Les dépendances
   s'installent seules et `config/config.toml` est créé, réglé pour écouter sur
   toutes les interfaces.
2. Dans le terminal du Codespace : `./start`
3. Ouvre le lien affiché — une adresse `https://<codespace>-8765.app.github.dev`
   qui contient déjà le jeton d'accès. Il fonctionne tel quel depuis un
   téléphone, connecté au même compte GitHub.

La connexion se fait alors par OAuth (méthode 3 ci-dessus) : les deux méthodes
fondées sur un navigateur local n'ont pas de sens dans un conteneur distant.

Deux réglages avant de commencer :

- **`DISCOGS_TOKEN`** en secret de Codespace (Settings → Codespaces →
  Repository secrets), sinon l'analyse ne peut pas interroger Discogs.
- **Garde le port 8765 en visibilité « Private »** : le lien reste alors lié à
  ton compte GitHub. Le jeton d'accès de l'application s'ajoute à cette
  protection, il ne la remplace pas.

Le Codespace s'arrête après une période d'inactivité et l'offre gratuite est
limitée en heures-machine. Le fichier d'authentification YouTube et la base
SQLite vivent dans le conteneur : les supprimer avec le Codespace impose de
reconnecter le compte et de relancer une analyse (les playlists déjà créées,
elles, restent sur ton compte).

### Accès depuis le réseau local

```bash
ytmgc web --host 0.0.0.0
```

Un jeton d'accès est alors **obligatoire** — l'application peut réécrire ta
bibliothèque, elle ne doit pas être ouverte à qui atteint le port. Il est
engendré au démarrage et inclus dans le lien affiché ; définis
`YTMGC_ACCESS_TOKEN` pour en garder un stable. `--no-token` lève l'exigence,
à ne faire que si l'accès est déjà protégé par ailleurs.

Le parcours tient en cinq étapes : connecter le compte, lancer l'analyse,
choisir un type de tri, examiner l'aperçu, confirmer. **Rien n'est écrit sur le
compte tant que la confirmation n'a pas été donnée**, et l'aperçu montre
exactement ce qui sera créé : nom de chaque playlist, nombre de titres,
description complète et échantillon de morceaux.

Une section « Annuler » supprime en un clic toutes les playlists générées et
rend le compte à son état initial.

L'interface est **locale par défaut** (`127.0.0.1`) : elle manipule les
identifiants de session YouTube Music, qui ne doivent jamais transiter par un
serveur tiers.

### Types de tri

| Mode | Résultat |
|---|---|
| `detaille` (défaut) | Une playlist par style, un titre pouvant relever de deux styles. |
| `exhaustif` | Le plus fin possible : chaque style représenté obtient sa playlist. |
| `sans-doublon` | Chaque titre n'apparaît que dans une playlist : une cartographie exacte. |
| `genre` | Une poignée de grandes playlists, sans détail de style. |

## Utilisation — ligne de commande

Le pipeline est découpé en étapes reprenables — chacune est persistée en base,
une interruption ne fait rien perdre :

```bash
ytmgc scan       # bibliothèque YouTube Music -> SQLite
ytmgc classify   # SQLite -> Discogs -> genres et styles
ytmgc plan       # playlists souhaitées (hors ligne, aucun appel réseau)
ytmgc apply      # simulation du diff
ytmgc apply --execute   # écriture réelle sur YouTube Music
ytmgc status     # avancement
ytmgc review     # appariements incertains, à vérifier à la main
ytmgc purge --execute   # supprime les playlists générées (annulation complète)
```

`plan` et `apply` acceptent `--tri` pour choisir un type de tri :
`ytmgc plan --tri sans-doublon`.

`apply` est **en mode simulation par défaut** : il faut `--execute` pour écrire.

## Garanties

- **Tes playlists manuelles ne sont jamais touchées.** L'outil ne modifie que
  les playlists dont la description porte son marqueur (`[ytmgc]`) ; tout le
  reste lui est invisible.
- **Idempotent.** Un second run sans changement ne produit aucune écriture.
- **Annulable.** `ytmgc purge` (ou le bouton « Annuler » de l'interface)
  supprime toutes les playlists générées, et seulement celles-là.
- **Économe en appels.** Le cache Discogs est indexé par (artiste, album) : une
  bibliothèque de 5 000 titres issus de 800 albums coûte ~800 requêtes, et zéro
  au run suivant. C'est ce qui rend supportable la limite de 60 requêtes/minute.
- **Retravaillable hors ligne.** La taxonomie Discogs est stockée brute :
  changer les seuils, les alias de styles ou le gabarit de nommage se rejoue
  avec `plan` et `apply`, sans un seul appel réseau supplémentaire.

## Limites connues

- `ytmusicapi` utilise l'API **interne** de YouTube Music : non officielle, elle
  peut changer sans préavis. Le fichier d'authentification expire régulièrement.
- Une playlist est plafonnée à **5 000 titres**.
- Discogs décrit des **releases**, pas des pistes : un morceau hérite du style de
  son album. Les compilations multi-styles sont donc classées approximativement.
- Les titres mis en ligne par l'utilisateur (*uploads*) sont rarement présents
  dans Discogs et finissent le plus souvent en `unmatched`.

## Réglages

Les deux réglages qui déterminent le résultat final :

| Réglage | Défaut | Effet |
|---|---|---|
| `multi_style` | `all` | `all` place un titre dans chaque playlist de style correspondante (vue la plus fidèle à Discogs) ; `primary` n'en retient qu'une. |
| `min_tracks_per_style` | `4` | Nombre de titres à partir duquel un style obtient sa playlist. Bas par défaut, pour nommer précisément ce qu'on écoute. |
| `min_tracks_per_genre` | `3` | Idem au niveau du genre. En deçà, les titres partent dans « Divers — genres isolés ». |

Un style sous le seuil rejoint la playlist `Genre — Autres styles`, dont la
description explique ce regroupement. `ytmgc plan` permet de balayer ces valeurs
sans aucune écriture ni appel réseau.

## Développement

```bash
python -m pytest        # 223 tests, aucun appel réseau
```

Les API externes sont derrière des adaptateurs (`src/ytmgc/sources/`) ; toute la
logique métier — appariement, taxonomie, planification, diff — est testée avec
des doubles en mémoire.

`tests/test_ui.py` charge en plus l'interface dans un vrai navigateur : le
câblage du DOM échappe aux tests Python. Ces tests sont ignorés si Playwright
ou son navigateur ne sont pas installés (`playwright install chromium`).

Voir [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
