# Architecture

## Vue d'ensemble

```
YouTube Music                                            Discogs
     │  get_library_songs / get_liked_songs                  │  /database/search
     ▼                                                       ▼
┌──────────────────┐    ┌───────────────┐   cache   ┌──────────────────┐
│ sources/ytmusic  │───►│ store (SQLite)│◄──────────│ sources/discogs  │
└──────────────────┘    └───────┬───────┘           └──────────────────┘
                                │ tracks + candidats
                                ▼
                        ┌───────────────┐
                        │  classifier   │  matching/ : normalize + scorer
                        └───────┬───────┘
                                │ classifications (genres/styles bruts)
                                ▼
                        ┌───────────────┐
                        │    planner    │  taxonomy/ : alias, seuils, nommage
                        └───────┬───────┘
                                │ PlaylistPlan[] (état souhaité)
                                ▼
                        ┌───────────────┐
                        │     sync      │  diff puis application par lots
                        └───────┬───────┘
                                ▼
                          YouTube Music
```

Chaque étape est un sous-commande du CLI et écrit son résultat en base. Le
pipeline est donc reprenable : `scan` puis `classify` peuvent être interrompus
et relancés sans perte, et `plan`/`apply` se rejouent hors ligne.

## Modules

| Module | Rôle |
|---|---|
| `models.py` | Structures partagées (`Track`, `ReleaseCandidate`, `Classification`, `PlaylistPlan`, `SyncAction`). Passives, sans logique. |
| `config.py` | Défauts < `config.toml` < environnement. Les secrets viennent uniquement de l'environnement. |
| `store/` | Schéma SQLite et accès typés. Aucun SQL ailleurs dans le projet. |
| `sources/ytmusic.py` | Adaptateur `ytmusicapi` : scan, lecture et écriture de playlists. |
| `sources/discogs.py` | Client HTTP Discogs : recherche, limitation de débit, reprises sur erreur. |
| `matching/` | Normalisation des libellés et scoring des candidats. Pur, sans état. |
| `taxonomy/` | Alias de styles, priorité des genres, nommage des playlists. |
| `classifier.py` | Orchestration titre → candidats → classification. |
| `planner.py` | Classifications → ensemble de playlists souhaité. Hors ligne. |
| `sync.py` | Diff état souhaité / état distant, puis application. |
| `cli.py` | Interface utilisateur. |

## Décisions structurantes

### 1. La hiérarchie vit dans le nom des playlists

YouTube Music n'a **pas de dossiers**. Le couple `(genre, style)` de Discogs est
rendu par le gabarit `{genre} — {style}` : le tri alphabétique de la
bibliothèque regroupe alors les styles d'un même genre. Une **clé stable**
(`electronic/deep-house`) est inscrite dans la description ; elle survit à un
renommage manuel et relie la playlist à son couple genre/style.

### 2. Le marqueur en description délimite ce que l'outil peut toucher

Seules les playlists dont la description contient `[ytmgc]` sont lues comme
gérées ; les autres sont ignorées par le diff. C'est la garantie que les
playlists faites à la main ne sont jamais modifiées. Une playlist gérée devenue
obsolète est **vidée, jamais supprimée** : l'API ne sait pas restaurer une
playlist, et un scan partiel ne doit pas détruire du travail.

### 3. Le cache Discogs est indexé par (artiste, album), pas par titre

L'API Discogs plafonne à 60 requêtes/minute avec jeton. Comme la taxonomie est
portée par la *release*, tous les titres d'un même album partagent une requête :
une bibliothèque de 5 000 titres issus de 800 albums coûte ~800 appels au
premier run, zéro ensuite (TTL 90 jours).

### 4. La taxonomie brute est stockée telle quelle

`classifications` conserve les genres et styles renvoyés par Discogs, sans
transformation. Les alias, les seuils et les gabarits de nommage ne sont
appliqués qu'à la planification : les retoucher se rejoue avec `plan` et
`apply`, sans un seul appel réseau supplémentaire.

### 5. Le scoring rejette la similarité de hasard

Trois composantes, chacune dans [0, 1] :

| Composante | Poids | Signal |
|---|---|---|
| `artist` | 0,50 | Meilleure correspondance entre un artiste crédité côté YouTube et l'artiste Discogs (maximum, pas moyenne : les collaborations ne sont pas créditées pareil des deux côtés). |
| `release` | 0,40 | Album YouTube ↔ release Discogs. Sans album, la recherche a porté sur la piste : la composante part d'une valeur neutre de 0,5, relevée si la release est éponyme du morceau. |
| `metadata` | 0,10 | Présence de styles, de genres, d'une année — une release sans taxonomie ne sert à rien. |

Toutes les similarités passent par un **seuil de confiance** à 0,5, réétalé sur
[0, 1]. Sans lui, deux chaînes courtes sans rapport (« Moon Safari » /
« Talkie Walkie ») obtiennent ~0,25 avec `SequenceMatcher`, et ce bruit suffit à
faire passer un mauvais appariement au-dessus du seuil d'acceptation.

Le score décide ensuite du statut : `matched` (≥ `min_score`), `review`
(≥ `review_score`, conservé mais hors playlists), `unmatched`.

### 6. Le repli en cascade évite l'émiettement

C'est l'écueil principal du projet : sans regroupement, une grosse bibliothèque
produit des centaines de playlists de deux titres, illisibles dans une interface
sans dossiers. Trois règles :

1. un style sous `min_tracks_per_style` n'a pas de playlist ;
2. le titre concerné rejoint alors **un autre de ses styles** s'il en a un qui,
   lui, a franchi le seuil (rattrapage) — sinon la playlist de son genre ;
3. un genre sous `min_tracks_per_genre` se replie sur la playlist fourre-tout.

`multi_style = "primary"` n'affecte chaque titre qu'une fois, en choisissant le
style le plus représenté dans la bibliothèque (à égalité, l'ordre Discogs
tranche : le premier style listé est le plus représentatif). `"all"` duplique le
titre dans chaque playlist de style, jusqu'à `max_styles_per_track`.

## Contraintes des API

| Contrainte | Conséquence |
|---|---|
| Discogs : 60 req/min authentifié | Seau de jetons (`ratelimit.py`) + cache par album. |
| Discogs : 429 et 5xx | Reprises avec repli exponentiel, `Retry-After` respecté. |
| YouTube Music : pas de dossier | Hiérarchie encodée dans le nom. |
| YouTube Music : 5 000 titres/playlist | Les seuils de repli maintiennent les playlists loin du plafond. |
| YouTube Music : suppression par `setVideoId` | `sync` relit la playlist avant tout retrait. |
| YouTube Music : API interne, non officielle | Toute la dépendance est isolée dans un seul adaptateur, importé paresseusement. |

## Tests

99 tests, aucun appel réseau. Les adaptateurs externes sont doublés en mémoire
(`tests/fakes.py`), y compris pour un test de bout en bout scan → classify →
plan → apply qui vérifie l'idempotence du second run et l'intégrité des
playlists manuelles.
