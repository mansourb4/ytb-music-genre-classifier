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
Electronic — Deep House

GENRE — Electronic
Musiques dont le son est produit ou transformé par des moyens électroniques —
synthétiseurs, boîtes à rythmes, échantillonneurs, ordinateurs. Recouvre aussi
bien la musique de club que l'écoute domestique et l'expérimentation.

STYLE — Deep House
Branche la plus soul de la house : tempo modéré, accords de septième et de
neuvième empruntés au jazz, nappes profondes et voix feutrées.

✱
```

Le `✱` final est la seule marque technique : c'est à lui que l'outil reconnaît
ses propres playlists. Il est configurable (`sync.marker`) — choisis un signe
que tu n'emploies pas toi-même. Un `config.toml` écrit avant ce changement,
portant encore `[ytmgc]`, est migré au chargement : rien à éditer à la main.

Ces définitions vivent dans `src/ytmgc/taxonomy/descriptions.toml` : 15 genres
et près de 200 styles.

Le tri par **ambiance** repose sur `src/ytmgc/taxonomy/moods.toml`, qui
rattache chacun de ces styles à l'une de huit humeurs — calme, mélancolique,
énergique, festif, planant, sombre, groovy, cérébral. Discogs ne décrivant
aucune humeur, cette table est un **jugement éditorial** : elle est faite pour
être corrigée, et modifier une ligne se rejoue avec `plan` sans un seul appel
réseau. Chaque playlist d'ambiance énumère en description les styles qu'elle
réunit, pour que le classement reste vérifiable. Un style non encore décrit reste parfaitement utilisable,
sa playlist est simplement créée sans définition — Discogs en ajoute
régulièrement.

Trois sources alimentent ce classement, de la moins à la plus précise :

| Source | Ce qu'elle décrit | Coût |
|---|---|---|
| **Discogs** | La *release*. Tous les titres d'un album en héritent identiquement. | Gratuit |
| **Last.fm** | Le *titre*, par les tags de ses auditeurs — où le genre écrase l'humeur. | Gratuit |
| **Modèle** | La *musique* elle-même, morceau par morceau, avec une justification. | ~6 $ une fois |

Chacune l'emporte sur la précédente quand elle a quelque chose à dire, et la
dernière est facultative : voir [la section dédiée](#clé-anthropic-facultative-payante--la-précision-réelle).

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

### Clé Last.fm (facultative, mais décisive pour la précision)

Discogs étiquette des **albums** : les douze titres d'un disque héritent
identiquement de ses styles, si bien qu'une ballade sur un album punk se
retrouve classée « Punk », donc « Énergique ». Last.fm est la seule source
encore ouverte qui décrive le **titre** lui-même.

Crée une clé sur <https://www.last.fm/api/account/create>, puis :

```bash
export LASTFM_API_KEY=...
```

Vérifie la clé avant de lancer une analyse complète :

```bash
ytmgc tags "Nirvana" "Something In The Way"
```

La commande affiche les tags du titre, ceux qui désignent un style, le poids
cumulé de chaque ambiance et celle qui l'emporte. Elle sert aussi après coup, pour comprendre pourquoi
un titre a atterri dans telle playlist.

Les tags servent à deux choses : remplacer les styles de l'album par ceux que
porte le morceau quand ils nomment un style connu, et déterminer son ambiance.
Seuls les tags nommant un style connu sont retenus — les tags libres
(« 00s », « seen live ») sont écartés par construction. Sans clé, l'outil
fonctionne comme avant, sur les seuls styles de la release.

### Clé Anthropic (facultative, payante — la précision réelle)

Discogs décrit le disque. Last.fm compile des tags posés par des auditeurs, où
le genre écrase l'humeur : *Something In The Way* y pèse `grunge 100` et
`sad 1`, alors que c'est une berceuse. Aucune des deux ne connaît la musique ;
elles en connaissent les étiquettes.

Un modèle, lui, connaît le morceau. La passe `enrich` lui soumet chaque titre
avec ce que les bases en disent — à titre d'indice, pas de consigne — et en
obtient un genre, un style et une ambiance **pour le titre lui-même**, avec une
phrase de justification.

```bash
pip install -e ".[claude]"
export ANTHROPIC_API_KEY=...          # jamais lu depuis config.toml

ytmgc enrich --dry-run                # annonce le coût, n'envoie rien
ytmgc enrich --essai                  # juge 50 titres (~0,12 $), pour voir
ytmgc enrich                          # la passe complète, après confirmation
```

**Commence par l'essai.** La qualité d'un classement se constate, elle ne se
promet pas : `--essai` juge 50 titres pour une poignée de centimes, tu relis
`data/verdicts.txt`, et tu engages la suite en connaissance de cause. Ces 50
verdicts ne sont pas redemandés ensuite. L'interface propose la même chose :
trois étendues — essai, titres non rangés, bibliothèque entière — chacune avec
son prix, l'essai coché par défaut.

**Ce que ça coûte.** Le traitement par lots est à moitié prix et aboutit en
général en quelques minutes (24 h au maximum garanti). Pour ~2 500 titres :
environ **6 $ en une seule fois**. Le montant exact est annoncé avant toute
dépense, dans le terminal comme dans l'interface, et rien n'est envoyé sans une
confirmation qui porte ce montant.

**Ce n'est payé qu'une fois.** Chaque verdict est écrit en clair dans
`data/verdicts.txt`, une ligne par titre :

```
Nirvana	Something In The Way	Rock	Acoustic	Mélancolique	0.92	claude	Berceuse sépulcrale, voix au bord du souffle.
```

Ce fichier **fait autorité** : un titre qui y figure n'est plus jamais envoyé à
l'API, et sa ligne l'emporte sur Discogs comme sur Last.fm. Il survit à la base
de données, se copie d'une machine à l'autre, se relit et **se corrige à la
main** — mets alors `manuel` en colonne source, et le modèle ne l'écrasera plus.

C'est le seul contenu de `data/` qui soit **versionné** : tout le reste (base
SQLite, caches, lot en cours) se reconstruit gratuitement, lui non. Il suit donc
le dépôt, et un `git clone` sur une autre machine retrouve des verdicts déjà
payés. Étant du texte, une ligne par titre, un `git diff` montre exactement ce
qu'une passe a changé.

**Pour les titres ajoutés après coup**, pas besoin de relancer une passe :

```bash
ytmgc lookup "Nirvana" "Something In The Way"
```

Une réponse immédiate à quelques centimes, écrite dans le même fichier. La
même recherche existe dans l'interface web, à l'étape 5.

Sans clé, tout le reste fonctionne exactement comme avant.

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

Le parcours tient en cinq étapes : connecter le compte, choisir ce qui sera
analysé (bibliothèque, titres likés, et n'importe laquelle de tes playlists),
choisir un type de tri, lancer l'analyse, confirmer. Pendant l'analyse, un
bandeau fixe indique l'avancement, le titre en cours et une estimation du temps
restant.

**L'aperçu s'affiche de lui-même à la fin de l'analyse**, dans la même section.
Chaque playlist proposée y porte sa pochette, la définition de son genre et de
son style, et se déplie sur la liste de ses titres — pochette, album, année,
genre et style pour chacun.

**Tout y est décochable** : une playlist entière, ou un titre à l'intérieur.
Seul ce qui reste coché est appliqué, et une playlist décochée qui existe déjà
sur le compte est laissée intacte plutôt que vidée.

Chaque analyse repart de zéro : l'aperçu reflète exactement les sources
cochées, sans se cumuler avec les analyses précédentes. Le cache Discogs, lui,
est conservé — c'est ce qui rend une réanalyse quasi immédiate. **Rien n'est écrit sur le
compte tant que la confirmation n'a pas été donnée**, et l'aperçu montre
exactement ce qui sera créé : nom de chaque playlist, nombre de titres,
description complète et détail de chaque morceau.

Une section « Annuler » liste d'abord les playlists que l'outil reconnaît comme
siennes — avec leur pochette et leur nombre de titres — et ne supprime que
celles restées cochées. Rien n'est effacé avant cette sélection.

L'interface est **locale par défaut** (`127.0.0.1`) : elle manipule les
identifiants de session YouTube Music, qui ne doivent jamais transiter par un
serveur tiers.

### Types de tri

| Mode | Résultat |
|---|---|
| `detaille` (défaut) | Une playlist par style, un titre pouvant relever de deux styles. |
| `exhaustif` | Le plus fin possible : chaque style représenté obtient sa playlist. |
| `sans-doublon` | Chaque titre n'apparaît que dans une playlist : une cartographie exacte. |
| `ambiance` | Range par humeur — calme, énergique, festif, groovy… — déduite des styles. |
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
ytmgc tags "Artiste" "Titre"   # tags d'un titre et classement qui en découle
ytmgc enrich     # fait juger les titres par le modèle (par lots, payant)
ytmgc lookup "Artiste" "Titre"  # genre, style et ambiance d'un titre, tout de suite
ytmgc purge --execute   # supprime les playlists générées (annulation complète)
```

`enrich` annonce son coût et demande confirmation. `--essai` se limite à une
poignée de titres (`claude.pilot_size`, 50 par défaut), `--dry-run` s'arrête à
l'annonce, `--only-unsorted` se limite aux titres que Discogs n'a pas su
classer, `--no-wait` rend la main après le dépôt et `--resume` va chercher un
lot déjà déposé — il vit chez Anthropic, l'ordinateur peut s'éteindre entre les
deux.

`plan` et `apply` acceptent `--tri` pour choisir un type de tri :
`ytmgc plan --tri sans-doublon`.

`apply` est **en mode simulation par défaut** : il faut `--execute` pour écrire.

## Garanties

- **Tes playlists manuelles ne sont jamais touchées.** L'outil ne modifie que
  les playlists dont la description porte son marqueur — un simple `✱` en
  dernière ligne ; tout le reste lui est invisible.
- **Idempotent.** Un second run sans changement ne produit aucune écriture.
- **Annulable, et jamais à l'aveugle.** L'interface montre les playlists
  détectées avant d'en supprimer aucune, et n'agit que sur la sélection.
  `ytmgc purge` fait de même en simulant par défaut. Une playlist sans le
  marqueur n'est jamais supprimée, même si son identifiant est explicitement
  demandé.
- **Économe en appels.** Le cache Discogs est indexé par (artiste, album) : une
  bibliothèque de 5 000 titres issus de 800 albums coûte ~800 requêtes, et zéro
  au run suivant. C'est ce qui rend supportable la limite de 60 requêtes/minute.
- **Jamais de dépense implicite.** La seule partie payante de l'outil
  (`enrich`, `lookup`) annonce son montant avant d'agir, exige une confirmation
  explicite, n'interroge jamais deux fois le même titre, et ne redépose jamais
  un lot déjà payé.
- **Retravaillable hors ligne.** La taxonomie Discogs est stockée brute :
  changer les seuils, les alias de styles ou le gabarit de nommage se rejoue
  avec `plan` et `apply`, sans un seul appel réseau supplémentaire.

## Limites connues

- `ytmusicapi` utilise l'API **interne** de YouTube Music : non officielle, elle
  peut changer sans préavis. Le fichier d'authentification expire régulièrement.
- Une playlist est plafonnée à **5 000 titres**.
- Discogs décrit des **releases**, pas des pistes : un morceau hérite du style de
  son album. Les compilations multi-styles sont donc classées approximativement.
  C'est la limite que `ytmgc enrich` lève, au prix d'une passe payante.
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
python -m pytest        # 419 tests, aucun appel réseau
```

Les API externes sont derrière des adaptateurs (`src/ytmgc/sources/`) ; toute la
logique métier — appariement, taxonomie, planification, diff — est testée avec
des doubles en mémoire.

`tests/test_ui.py` charge en plus l'interface dans un vrai navigateur : le
câblage du DOM échappe aux tests Python. Ces tests sont ignorés si Playwright
ou son navigateur ne sont pas installés (`playwright install chromium`).

Voir [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
