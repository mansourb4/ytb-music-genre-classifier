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
| **Modèle** | La *musique* elle-même, morceau par morceau, avec une justification. | Gratuit via Claude.ai, ou ~0,25 centime par titre via l'API |

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

### Juger les morceaux avec Claude — gratuitement, via Claude.ai

Discogs décrit le disque et Last.fm des étiquettes ; seul un modèle connaît le
morceau lui-même. Avec un compte Claude.ai, cette passe ne coûte rien.

**1. Exporte.** Étape 4 de l'interface, onglet « Gratuit — via Claude.ai »,
*Exporter les titres à juger* — ou `ytmgc export`. Tous les titres pas encore
jugés partent d'un coup, en fichiers de 500 (`titres-1-sur-9.txt`…), chacun
autonome : consignes, vocabulaire, titres.

**2. Une conversation par fichier.** Nouvelle conversation sur claude.ai,
glisse le fichier, écris « Vas-y » ; « continue » si Claude s'arrête avant la
fin. Pourquoi pas tout dans une seule conversation : elle garde en mémoire les
titres *et* toutes ses réponses — pour ~4 000 titres, environ 220 000 jetons,
au-delà de ce qu'elle tient. Pour 500, une trentaine de milliers.

**3. Recolle chaque réponse**, dans la case prévue ou un fichier déposé — ou
`pbpaste | ytmgc importe`. Dans n'importe quel ordre, en autant de fois que tu
veux : Claude répond une ligne par morceau,

```
Nirvana | Something In The Way | Rock | Acoustic | Mélancolique | 0.95 | Berceuse sépulcrale.
```

et chaque ligne porte le nom de son morceau. Pas de numéro, pas d'état à
retrouver : une réponse d'il y a trois jours s'importe comme celle d'il y a
trois minutes.

**Où en es-tu.** Le bloc *Résultats*, en tête de l'étape 4, dit combien de
morceaux sont jugés, **où** ils sont enregistrés (le chemin complet de
`data/verdicts.txt`) et liste chaque morceau — jugé, avec genre, style,
ambiance et note, ou à juger — avec un filtre et une recherche. Chaque fichier
exporté affiche aussi son avancement (« 312 / 500 jugés »). En ligne de
commande : `ytmgc suivi`, `ytmgc suivi --faits`, `ytmgc suivi --a-faire`.

**Ce qui n'est pas retenu.** Une ligne qui ne désigne aucun morceau de ta
bibliothèque, ou dont le genre ou l'ambiance sortent des listes, est écartée
et affichée avec sa raison. Un verdict rattaché au mauvais morceau serait pire
qu'un verdict manquant — il serait tenu pour acquis. Le morceau reste « à
juger » et figurera dans le prochain export.

### Clé Anthropic (facultative, payante — la même chose, automatiquement)

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

**Un morceau n'est payé qu'une fois, même publié sous plusieurs formes.**
YouTube Music présente souvent une même chanson comme titre d'album, comme clip
(« Nirvana - Smells Like Teen Spirit (Official Music Video) »), depuis la chaîne
« Nirvana - Topic » ou « NirvanaVEVO ». Toutes ces formes sont reconnues comme un
seul morceau, jugé une fois ; seul l'artiste principal compte, les invités
variant d'une publication à l'autre. Les versions qui sonnent autrement — live,
remix, acoustique — restent distinctes. `ytmgc doublons` montre ce regroupement
sur ta bibliothèque, sans aucun appel réseau.

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
même recherche existe dans l'interface web, à l'étape 4.

Sans clé, tout le reste fonctionne exactement comme avant.

### Jeton Discogs

Crée un jeton personnel sur <https://www.discogs.com/settings/developers>, puis :

```bash
cp .env.example .env   # puis renseigne DISCOGS_TOKEN, et LASTFM_API_KEY si tu en as une
```

Le fichier `.env`, à la racine du projet, est **lu à chaque démarrage** de
`ytmgc` : rien à réexporter quand tu ouvres un nouveau terminal. Une variable
définie dans le terminal (`export DISCOGS_TOKEN=…`) l'emporte sur lui. Les
jetons ne sont **jamais** lus depuis `config.toml`, et `.env` n'est pas
versionné : ils ne finissent pas sur GitHub.

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

Le parcours suit l'ordre réel du travail, chaque étape ne dépendant que des
précédentes :

1. **Connecter** le compte.
2. **Choisir ce qui sera analysé** : bibliothèque, titres likés, n'importe
   laquelle de tes playlists — et les playlists à **exclure**.
3. **Analyser** : lecture des sources, Discogs, Last.fm. Un bandeau fixe
   indique l'avancement, le titre en cours et le temps restant.
4. **Affiner par le modèle** (facultatif) : Claude juge chaque morceau.
5. **Choisir le type de tri** : il ne change rien à ce qui précède, seulement
   la façon d'en faire des playlists ; l'aperçu suit chaque changement.
6. **Prévisualiser**, puis 7. **Appliquer**.

**Exclure une playlist** n'est pas la même chose que la décocher. Décochée, une
playlist n'est simplement pas lue — mais ses titres reviennent par ta
bibliothèque ou tes likes. Exclue, ses titres sont retirés de l'analyse d'où
qu'ils viennent, clips et autres publications du même morceau compris. C'est
fait pour une playlist de berceuses, de bruit blanc, de sons de méditation :
ce qui n'a rien à faire dans le portrait de ce que tu écoutes. Le choix est
gardé d'une analyse à l'autre, et `ytmgc scan` l'applique aussi.

**L'aperçu se calcule de lui-même** à la fin de l'analyse, à chaque changement
de tri et à chaque import de verdicts.
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
| `familles` (défaut) | Un plan de playlists écrit à l'avance (`config/playlists.toml`) : chaque titre va dans la première playlist dont la règle lui correspond. |
| `detaille` | Une playlist par style, un titre pouvant relever de deux styles. |
| `exhaustif` | Le plus fin possible : chaque style représenté obtient sa playlist. |
| `sans-doublon` | Chaque titre n'apparaît que dans une playlist : une cartographie exacte. |
| `ambiance` | Range par humeur — calme, énergique, festif, groovy… — déduite des styles. |
| `genre` | Une poignée de grandes playlists, sans détail de style. |

### Le plan de playlists (tri « Par famille »)

Les autres tris *déduisent* les playlists des données : un style devient une
playlist dès qu'il atteint un seuil. Sur une bibliothèque de 4 000 titres, cela
donne une centaine de playlists, la bossa nova coupée entre Jazz, Latin et Pop,
et des fourre-tout dont on ignore le contenu.

Le tri « Par famille » fait l'inverse : `config/playlists.toml` dit quelles
playlists existent et ce qu'elles acceptent. Deux principes le guident :

1. **Comprendre ce qu'on écoute** : chaque playlist porte une famille réelle
   (« Brésil · Bossa nova », « Maghreb · Gnawa », « Jazz · Ethio-jazz »),
   jamais un fourre-tout comme « Monde ».
2. **Garder la même ambiance** du premier au dernier titre : une famille qui
   mêle des ambiances est découpée — le plus souvent en une playlist vive
   (groovy, festive, énergique) et une douce (calme, planante, mélancolique) :
   « Soul · Soul groovy » et « Soul · Soul ballades », « Classique · Baroque
   vif » et « Classique · Baroque lent ». Aucune playlist « Divers ».

```toml
[taille]          # l'aperçu signale les playlists hors de cette plage
min = 30
max = 150

[[playlist]]
nom = "Rap · Trap énergique"
description = "Trap et drill qui cognent."
genres = ["Hip Hop"]
styles = ["Trap", "Drill"]
ambiances = ["Énergique", "Festif", "Groovy"]

[[playlist]]
nom = "Rap · Trap sombre"
genres = ["Hip Hop"]
styles = ["Trap", "Drill"]
```

* **La première règle qui correspond l'emporte**, en lisant le fichier de haut
  en bas : une règle étroite placée avant une large prend ce qui la concerne.
  Un titre ne figure donc que dans une playlist.
* Un critère absent accepte tout ; plusieurs blocs du même nom alimentent la
  même playlist (« Jazz-Funk, ou bien Fusion quand elle groove »).
* Un critère `artistes = [...]` range par artiste — invités et chaînes YouTube
  compris. C'est ainsi que Maghreb, Orient ou Afrique sont rangés : le
  verdict dit « Ballad » ou « Traditional », pas Beyrouth ou Bamako. Un artiste
  nommé mais absent de la bibliothèque (faute de frappe ?) est signalé.
* Genres et ambiances sont vérifiés au chargement : une faute de frappe est
  signalée au lieu de faire taire la règle.
* **Aucun fourre-tout silencieux** : un titre qu'aucune règle n'accepte figure
  dans « non rangés », avec son genre, son style et son ambiance — de quoi
  écrire la règle qui manque.
* L'aperçu montre, pour chaque titre, la note de Claude, et marque « à
  vérifier » ceux dont le verdict est peu sûr (`claude.unsure_below`).
* L'aperçu relit `data/verdicts.txt` à chaque calcul : une ligne corrigée à la
  main s'y voit aussitôt, sans relancer l'analyse.

**Déplacer un titre.** Dans l'aperçu, chaque titre porte un bouton
« Déplacer » : il ouvre les playlists du plan regroupées par famille (Funk,
Jazz, Rap…), avec un champ pour chercher par nom — sans se soucier des
accents —, plus « Ne ranger nulle part » et « Rendre aux règles » pour un
titre déjà déplacé. Entrée choisit la première playlist trouvée. Les titres
non rangés ont le même bouton, et un ▶ ouvre chaque titre dans YouTube Music.
Chaque playlist y affiche son nombre de titres. Le **✕** à côté retire un
titre dont on ne veut finalement pas : il n'est rangé nulle part et passe
dans les non rangés, d'où « Rendre aux règles » le remet. Il reste dans ta
bibliothèque YouTube Music. Un déplacement l'emporte sur les règles et tient après chaque analyse ;
il est écrit dans `data/placements.txt` (`artiste <tab> titre <tab> playlist`),
versionné comme les verdicts et modifiable à la main. Toutes les publications
d'un même morceau suivent le déplacement. Si la playlist visée disparaît du
plan, l'aperçu le signale et le titre suit les règles en attendant.

**Relecture par Claude — plutôt que de vérifier chaque titre.** Dans l'étape
6, « Relecture par Claude » exporte toutes tes playlists en quelques fichiers
(`data/export/relecture/`). Glisse chacun dans une nouvelle conversation
claude.ai, écris « Vas-y », et recolle la réponse : Claude relit chaque
playlist en entier et ne signale que les titres qui en cassent la vibe, avec
la playlist où ils iraient mieux. Ses propositions s'affichent, groupées par
playlist : « Accepter » déplace le titre, « Autre playlist » le range dans
celle de ton choix, « Garder ici » le laisse et valide son verdict, « Tout accepter » tranche une playlist d'un coup. Ce que tu as
déjà déplacé ou validé n'est jamais proposé. Les propositions en attente sont
dans `data/propositions.txt`.

Une fois la relecture faite, le panneau « titres à vérifier » propose
**« Tout valider »** : les verdicts peu sûrs que Claude a relus dans leur
playlist sans rien signaler passent en « manuel » d'un coup, après
confirmation. Ceux qu'une proposition attend restent en dehors.

**Créer une playlist.** Un titre qui n'a sa place nulle part : « ＋ Nouvelle
playlist… » après « Déplacer » — ou tape son nom dans la recherche, puis
« Créer la playlist » — (ou le bouton « ＋ Nouvelle
playlist » de l'étape 6) ouvre une fenêtre où l'on donne un nom — de
préférence « Famille · Nom », pour qu'elle se range avec les autres — et une
description facultative. La playlist est ajoutée en fin de
`config/playlists.toml`, avec `manuelle = true` : elle n'a pas de règle, seuls
les titres qu'on y range la remplissent. On peut lui en écrire une plus tard,
en éditant le fichier.

**Les playlists voisines, en un clic.** Sous chaque titre, « Aussi possible »
propose jusqu'à trois autres playlists où il aurait sa place, sous forme de
boutons. Elles sont choisies d'après ce que chaque playlist contient : d'abord
les autres titres du même artiste, puis le même style, le même genre, et
l'ambiance pour départager. Un clic y range le titre. Un déplacement met
l'aperçu à jour sur place : seul le titre bouge, sans recalcul ni relecture du
compte YouTube Music.

**Le rap, par pays.** Le rap se range en Rap · FR (France, Belgique, Suisse),
US (avec le Canada), UK (avec l'Irlande), Maghreb et Ailleurs. Le rap
francophone, le plus fourni, est en plus découpé par ambiance : FR énergique,
FR sombre, FR groovy & posé, FR mélancolique. Le pays de
chaque artiste vient de `data/pays.txt` (`artiste <tab> code pays`),
versionné et modifiable à la main ; une règle le désigne par
`pays = ["FR", "BE"]`. Un artiste sans pays connu atterrit dans « Rap · Pays à
préciser » (`pays = ["?"]`), où ses boutons proposent une playlist par pays
(FR, US, Ailleurs…) en un clic — ou ajoute une ligne au fichier pour ranger tous ses titres d'un coup.

**Relire les verdicts peu sûrs.** Le panneau « à vérifier » de l'aperçu
regroupe par artiste les titres que Claude ne connaissait pas. Pour chacun :
▶ pour l'écouter, « ✓ C'est bon » pour valider — le verdict passe en source
`manuel` dans `data/verdicts.txt` —, ou un déplacement. Un artiste se valide
ou se déplace d'un geste. Un titre validé ou déplacé sort de la liste, pour
de bon.

Il n'y a pas de playlist « BO » : une musique de film ou de jeu est rangée
selon sa musique (orchestre, jazz, synthés…). « BO & Scène » n'est donc plus
proposé à Claude comme genre.

Le plan livré a été calibré sur une bibliothèque réelle : 75 playlists, 4 078
titres rangés sur 4 083. Une petite playlist cohérente est préférée à une
grande qui change d'ambiance : seules les plus de 150 titres sont signalées. Modifie-le
librement, puis recalcule l'aperçu. Renommer une playlist en crée une nouvelle
à l'application : l'ancienne est vidée.

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
ytmgc doublons   # morceaux présents sous plusieurs formes (gratuit, hors ligne)
ytmgc export     # tous les titres à juger, en fichiers pour Claude.ai (gratuit)
ytmgc importe    # lit une réponse de Claude.ai
ytmgc suivi      # ce qui est jugé, ce qui reste, où sont les résultats
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

Le tri « Par famille » ne dépend que de `config/playlists.toml` (voir plus
haut). Pour les autres tris, les deux réglages qui déterminent le résultat final :

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
python -m pytest        # 507 tests, aucun appel réseau
```

Les API externes sont derrière des adaptateurs (`src/ytmgc/sources/`) ; toute la
logique métier — appariement, taxonomie, planification, diff — est testée avec
des doubles en mémoire.

`tests/test_ui.py` charge en plus l'interface dans un vrai navigateur : le
câblage du DOM échappe aux tests Python. Ces tests sont ignorés si Playwright
ou son navigateur ne sont pas installés (`playwright install chromium`).

Voir [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
