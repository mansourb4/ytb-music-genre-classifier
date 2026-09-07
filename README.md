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
Electronic — Ambient
Electronic — Deep House
Electronic — Techno
Rock — Grunge
Rock — Post-Punk
```

## Installation

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

### Authentification YouTube Music

`ytmusicapi` s'authentifie avec les en-têtes de ta session navigateur :

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

## Utilisation

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
```

`apply` est **en mode simulation par défaut** : il faut `--execute` pour écrire.

## Garanties

- **Tes playlists manuelles ne sont jamais touchées.** L'outil ne modifie que
  les playlists dont la description porte son marqueur (`[ytmgc]`) ; tout le
  reste lui est invisible.
- **Idempotent.** Un second run sans changement ne produit aucune écriture.
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

## Développement

```bash
python -m pytest        # 99 tests, aucun appel réseau
```

Les API externes sont derrière des adaptateurs (`src/ytmgc/sources/`) ; toute la
logique métier — appariement, taxonomie, planification, diff — est testée avec
des doubles en mémoire. Voir [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
