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
| `sources/lastfm.py` | Tags posés par les auditeurs sur un titre précis : la seule source décrivant le morceau et non le disque. |
| `sources/oauth.py` | Connexion OAuth par code d'appareil. |
| `sources/browser_session.py` | Reprise d'une session de navigateur, analyse des collages (cURL ou en-têtes bruts), et construction du fichier attendu par `ytmusicapi`. |
| `sources/browser_login.py` | Fenêtre de connexion pilotée, qui capture la session une fois l'utilisateur identifié. |
| `sources/discogs.py` | Client HTTP Discogs : recherche, limitation de débit, reprises sur erreur. |
| `matching/` | Normalisation des libellés et scoring des candidats. Pur, sans état. |
| `taxonomy/` | Alias de styles, priorité des genres, nommage, et définitions des genres et styles (`descriptions.toml`). |
| `classifier.py` | Orchestration titre → candidats → classification. |
| `planner.py` | Classifications → ensemble de playlists souhaité. Hors ligne. |
| `origins.py` | Pays d'origine des artistes (`data/pays.txt`), pour les règles `pays` du plan. |
| `playlist_review.py` | Relecture par playlist : playlists exportées en fichiers pour Claude.ai, lecture des intrus signalés, propositions en attente (`data/propositions.txt`). |
| `placements.py` | Déplacements faits à la main dans l'aperçu : fichier `data/placements.txt`, résolution par morceau, priorité sur les règles. |
| `playlist_plan.py` | Tri « Par famille » : lecture et vérification de `config/playlists.toml`, affectation de chaque morceau à la première playlist dont une règle l'accepte. |
| `sync.py` | Diff état souhaité / état distant, puis application. |
| `sorting.py` | Types de tri prédéfinis — dont l'axe de rangement — partagés par le CLI et l'interface web. |
| `sources/claude.py` | Jugement d'un titre par un modèle : préambule, schéma de sortie, dépôt et relecture d'un lot, estimation du coût. |
| `verdicts.py` | Le fichier texte des verdicts : format, lecture tolérante, écriture atomique, préséance de la décision humaine. |
| `exclusions.py` | Playlists exclues : leurs titres, et les autres publications des mêmes morceaux, sortent de l'analyse. |
| `duplicates.py` | Ce que l'outil tient pour un même morceau : doublons fusionnés, versions proches gardées à part. Diagnostic, sans réseau. |
| `chat.py` | Passe gratuite : bibliothèque exportée en fichiers pour Claude.ai, lecture défiante des réponses recollées. |
| `progress.py` | Ce qui est jugé, ce qui reste, et le verdict de chaque morceau. |
| `enrich.py` | La passe modèle de bout en bout : ce qui reste à juger, dépôt, attente, récupération, report en base. |
| `preview.py` | Assemble plan, état distant et titres en un aperçu sérialisable — pochettes et taxonomie comprises. Ni HTTP ni terminal. |
| `web/` | Application FastAPI et son interface (page unique, sans build), contrôle d'accès et construction des liens. |
| `cli.py` | Interface en ligne de commande. |

## Décisions structurantes

### 1. La hiérarchie vit dans le nom des playlists

YouTube Music n'a **pas de dossiers**. Le couple `(genre, style)` de Discogs est
rendu par le gabarit `{genre} — {style}` : le tri alphabétique de la
bibliothèque regroupe alors les styles d'un même genre. Une **clé stable** (`electronic/deep-house`) relie chaque playlist à son couple
genre/style. Elle ne figure plus dans la description, devenue purement
lisible : elle est tenue en base à la création, avec deux replis — le nom
attendu de la playlist, calculé depuis le plan, puis la description elle-même
pour les playlists de l'ancien format. Une playlist gérée qu'aucun de ces
moyens ne rattache est laissée telle quelle plutôt que modifiée au jugé.

La suppression, elle, ne dépend que du marqueur : une playlist gérée doit
rester supprimable même quand plus rien ne permet de la rattacher à un genre.

### 2. La description explique ce que la playlist contient

Le but du projet n'est pas seulement de ranger, mais de comprendre ce qu'on
écoute. La description de chaque playlist définit donc son genre et son style,
à partir de `taxonomy/descriptions.toml` (15 genres, près de 200 styles). Les
playlists de regroupement (`Genre — Autres styles`, `Divers — genres isolés`)
expliquent en plus la raison de leur existence.

Deux conséquences sur le nommage : les alias de styles sont limités aux
variantes d'écriture — confondre Jungle et Drum & Bass effacerait l'information
recherchée — et les seuils de repli sont bas par défaut (4 titres pour un style,
3 pour un genre), pour privilégier la précision au regroupement.

La description est aussi le support technique du marqueur et de la clé ; les
chevrons y sont remplacés, YouTube Music les refusant.

### 3. Le marqueur en description délimite ce que l'outil peut toucher

Seules les playlists dont la description porte le marqueur — un `✱` en dernière
ligne — sont lues comme gérées ; les autres sont ignorées par le diff. L'ancien
marqueur `[ytmgc]` reste reconnu, pour ne pas orpheliner les playlists créées
avant que la description ne devienne lisible. C'est la garantie que les
playlists faites à la main ne sont jamais modifiées. Une playlist gérée devenue
obsolète est **vidée, jamais supprimée** : l'API ne sait pas restaurer une
playlist, et un scan partiel ne doit pas détruire du travail.

### 4. Le cache Discogs est indexé par (artiste, album), pas par titre

L'API Discogs plafonne à 60 requêtes/minute avec jeton. Comme la taxonomie est
portée par la *release*, tous les titres d'un même album partagent une requête :
une bibliothèque de 5 000 titres issus de 800 albums coûte ~800 appels au
premier run, zéro ensuite (TTL 90 jours).

### 5. La taxonomie brute est stockée telle quelle

`classifications` conserve les genres et styles renvoyés par Discogs, sans
transformation. Les alias, les seuils et les gabarits de nommage ne sont
appliqués qu'à la planification : les retoucher se rejoue avec `plan` et
`apply`, sans un seul appel réseau supplémentaire.

### 6. Le scoring rejette la similarité de hasard

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

### 7. Le repli en cascade évite l'émiettement

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

### 8. L'écriture est toujours précédée d'un aperçu, et toujours annulable

`preview.build_preview` calcule hors ligne ce que deviendra le compte, à partir
du plan, de l'état distant et des titres concernés. Le CLI l'affiche, l'API web
le sérialise ; l'écriture réutilise le même calcul. Aucun appel d'écriture n'a
lieu sans confirmation explicite (`--execute`, ou `confirm: true` côté API).

`sync.purge` fournit l'annulation : suppression des playlists portant le
marqueur, et d'elles seules. C'est la seule opération destructrice du projet et
elle est irréversible côté YouTube Music, d'où trois précautions plutôt qu'une :
la confirmation explicite, la présentation préalable des playlists détectées
(`GET /api/purge/candidates`, qui ne supprime rien), et une sélection
transmise en identifiants. Le filtre par marqueur s'applique malgré tout à
cette sélection : un identifiant désignant une playlist non gérée n'est jamais
supprimé, quelle que soit la demande.

### 9. La base est protégée par un verrou

L'interface web exécute les traitements longs dans un fil de fond tout en
servant les requêtes d'état. La connexion SQLite est donc ouverte avec
`check_same_thread=False`, et `Repository` sérialise tous ses accès : c'est le
seul point d'entrée de la base, ce qui rend la garantie tenable.

### 10. L'exposition hors boucle locale impose un jeton

L'application pilote un compte YouTube Music : la joindre, c'est pouvoir
réécrire une bibliothèque. Tant qu'elle n'écoute que sur `127.0.0.1`,
l'isolement réseau suffit et aucune friction n'est imposée. Dès que `web.host`
sort de la boucle locale — réseau local, port transféré d'un Codespace — un
jeton devient obligatoire, et l'application **refuse de démarrer** sans lui
plutôt que de servir un accès ouvert.

Le jeton est accepté en paramètre d'URL (pour qu'un lien soit ouvrable tel quel
sur un téléphone), puis déposé en cookie `HttpOnly` ; la comparaison passe par
`secrets.compare_digest`. Il s'ajoute à l'authentification de l'hébergeur —
celle de GitHub sur un port transféré privé — sans la remplacer : il couvre le
cas où ce port passerait en visibilité publique.

### 11. Quatre voies de connexion, parce qu'aucune n'est fiable partout

YouTube Music n'a pas d'API publique. L'API officielle YouTube Data v3
n'expose pas la bibliothèque YouTube Music, et son quota (10 000 unités par
jour, 50 par insertion dans une playlist) plafonne à environ 200 titres ajoutés
par jour : inutilisable pour ranger une bibliothèque entière. Aucune connexion
« officielle » n'est donc possible, et chaque contournement a son domaine de
validité — d'où quatre voies, présentées de la plus simple à la plus manuelle.

**Session du navigateur.** Les cookies nécessaires sont déjà dans le navigateur
de l'utilisateur. `ytmusicapi` recalcule l'en-tête `authorization` à chaque
requête à partir du cookie `__Secure-3PAPISID` ; le fichier écrit en contient
néanmoins un, car c'est à sa présence que la bibliothèque reconnaît une
authentification de type navigateur plutôt qu'OAuth. Limite : Chrome chiffre
ses cookies sous Windows depuis la version 127.

**Fenêtre de connexion.** Un navigateur piloté est ouvert sur YouTube Music et
la session est relevée dès que le cookie de signature apparaît. Le profil est
persistant, ce qui évite de rejouer une connexion que Google pourrait refuser —
il bloque régulièrement l'authentification dans un navigateur automatisé.

**Collage manuel.** Le geste retenu est « Copier comme cURL » : c'est la seule
action identique dans Firefox, Chrome, Edge et Safari — une entrée de menu,
sans bouton à dénicher ni panneau à déplier, contrairement au bouton *Raw* des
en-têtes qui n'existe pas dans toutes les versions. L'application analyse aussi
bien une commande cURL (guillemets bash, cmd ou PowerShell) qu'une liste
d'en-têtes bruts, et n'en retient que le cookie de session : le reste est
reconstruit. Exiger `x-goog-authuser` obligeait à dénicher une requête d'API
précise ; s'en passer rend n'importe quelle requête authentifiée exploitable.

**OAuth par code d'appareil.** Le flux « limited input » de Google : une URL,
un code, une validation sur n'importe quel appareil. Seule voie indépendante
d'un navigateur local, donc la seule utilisable dans un conteneur distant.

La contrepartie est que Google exige un identifiant client OAuth propre à
l'application, que l'utilisateur crée lui-même. Il est traité comme un secret
local : jamais versionné, fichier en 0600, lisible aussi depuis
l'environnement.

Deux pièges spécifiques au flux, couverts par des tests : le code d'appareil
est **à usage unique** (l'échanger deux fois échoue), et les réponses
« authorization_pending » et « slow_down » ne sont pas des erreurs mais l'état
normal tant que l'utilisateur n'a pas validé.

### 12. L'analyse est bornée par l'utilisateur, et son avancement visible

Les sources ne sont plus figées dans la configuration : l'interface liste la
bibliothèque, les titres likés, les mises en ligne et chaque playlist du
compte, et n'analyse que la sélection. Les playlists engendrées par l'outil
sont écartées de cette liste — les analyser reviendrait à reclasser sa propre
sortie. Une sélection vide est refusée plutôt que remplacée par les valeurs de
la configuration : tout décocher produirait sinon l'inverse du geste exprimé.

Le bandeau sert aussi aux opérations sans progression mesurable — le calcul de
l'aperçu, la recherche des playlists générées — sous forme de barre défilante :
ce sont des requêtes uniques, dont on ne connaît que le début et la fin. Un
traitement de fond en cours garde la priorité sur le bandeau, son avancement
étant, lui, réellement chiffré.

Le suivi d'un traitement distingue deux phases. La lecture des sources n'a pas de total
connu — une barre défilante dit que le travail avance ; l'appariement Discogs,
lui, connaît son total et affiche le décompte et une estimation par règle de
trois. Le bandeau est en position fixe : placé en fin de page, il se trouvait
hors écran au moment précis où l'utilisateur attend un signe de vie.

### 13. L'aperçu conclut l'analyse, et montre la matière

L'aperçu s'affiche de lui-même à la fin de l'analyse plutôt que d'attendre
un second geste : une analyse dont le résultat reste invisible ne conclut rien.

Le tri a d'abord été placé *avant* l'analyse, au motif qu'il déterminait ce
qu'elle produirait. C'était faux : l'analyse ne lit pas le tri — elle produit
genres, styles et ambiances, et le tri ne dit que comment en faire des
playlists. Une fois la passe modèle ajoutée, l'ordre affiché trompait
franchement : l'aperçu venait avant les verdicts qui le modifient. La page suit
désormais l'ordre réel des dépendances — connecter, choisir les sources,
analyser, affiner, choisir le tri, prévisualiser, appliquer — et l'aperçu se
recalcule à chaque changement de tri comme à chaque import de verdicts.

Chaque playlist proposée porte la pochette de son premier titre illustré — les
playlists prévues n'existant pas encore, elles n'ont pas d'image propre — et se
déplie sur le détail de ses morceaux : pochette, album, année, genre et style.
Ces lignes ne sont construites qu'à l'ouverture : une bibliothèque fournie en
représenterait des milliers, inutiles tant qu'elles restent repliées.

Deux champs ont été ajoutés au stockage pour cela : la pochette d'un titre et
l'année de la release appariée. Les bases déjà remplies sont complétées par
`ALTER TABLE` au démarrage, sans perdre les heures d'analyse qu'elles
contiennent.

### 13 bis. Ce qui n'est pas rangé se dit, avec sa cause

Quatre filtres successifs écartent des titres du plan — jamais analysé, aucune
correspondance Discogs, appariement jugé trop incertain, matière sans genre ni
ambiance exploitables — et aucun ne laissait de trace : la bibliothèque finale
contenait moins de titres que le compte, sans que rien n'explique l'écart. Un
outil dont l'objet est de comprendre ce qu'on écoute ne peut pas perdre des
morceaux en silence.

`preview.build_preview` compare donc l'ensemble des titres au plan et remonte
les absents dans `unsorted`, chacun avec sa cause, plus un décompte par cause
(`unsorted_by_reason`) et les libellés correspondants (`reason_labels`, pour
que l'interface n'ait pas à les redire). La page les présente en un bloc
repliable, groupés par cause : c'est la cause, non le titre, qui dicte le geste
à faire — relancer l'analyse, corriger un appariement, ou constater que Discogs
et Last.fm ne connaissent tout simplement pas ce morceau.

En mode ambiance les causes diffèrent, Discogs n'y étant plus qu'un appoint :
soit le titre n'a ni tag Last.fm ni style (`no_tags`, rien à exploiter), soit
cette matière existe mais n'a donné aucune ambiance (`no_mood`).

### 13 ter. Exclure n'est pas décocher

Décocher une playlist à l'étape des sources ne fait que ne pas la lire : ses
titres reviennent par la bibliothèque ou les likes. Une playlist de berceuses
ou de bruit blanc continuait donc de peser dans le portrait de ce qu'on écoute.
`exclusions.apply_exclusions` retire de l'analyse tout titre d'une playlist
exclue, et tout autre titre du même morceau (`song_key`) — le clip d'une
comptine arrivé par les likes compris.

Les exclusions sont tenues dans `meta`, que l'effacement de la bibliothèque
avant chaque analyse épargne : c'est un choix durable. L'interface les
enregistre dès le clic, sans attendre l'analyse, et `ytmgc scan` les applique
aussi — retirant au passage de la base un titre lu avant son exclusion, `scan`
ne vidant pas la bibliothèque.

### 14. L'aperçu est un plan négociable, pas un compte rendu

Ce que montre l'aperçu peut être amendé avant écriture : chaque playlist et
chaque titre porte une case. La sélection est transmise en **exclusions**
(`excluded_playlists`, `excluded_tracks`), appliquées au plan par
`preview.filter_plans` ; une playlist vidée de tous ses titres disparaît du
plan, la créer vide n'ayant aucun sens.

Une playlist décochée qui existe déjà sur le compte est **laissée intacte**, ni
mise à jour ni vidée : `diff` reçoit pour cela un ensemble `untouched` qui
l'exclut aussi du balayage de nettoyage. Décocher signifie « n'y touche pas »,
pas « efface-la » — l'interprétation inverse détruirait du travail sur un
simple geste de tri.

L'analyse, elle, repart de zéro (`Repository.clear_library`) : l'aperçu doit
décrire les sources cochées, non l'union de toutes les analyses passées. Le
cache Discogs survit à cette remise à zéro, ce qui rend l'opération peu
coûteuse.

Enfin la description n'est plus affichée telle qu'elle sera écrite, mais
décomposée — genre, style, définitions, raison d'être d'une playlist de
regroupement — pour être lue plutôt que déchiffrée.

### 15. Un second axe de rangement : l'ambiance

Discogs ne décrit aucune humeur, et l'API de YouTube Music n'expose les siennes
que sous forme de playlists éditoriales, sans attribut par titre. L'ambiance
est donc **déduite des styles**, par une table (`taxonomy/moods.toml`) qui
rattache chacun des styles décrits à l'une de huit humeurs.

C'est un jugement éditorial, et il est présenté comme tel : chaque playlist
d'ambiance énumère en description les styles qu'elle réunit, de sorte que le
classement puisse être contesté par qui le lit. La table se corrige et se
rejoue hors ligne, comme les alias.

L'intégration tient en un choix de résolveur : `Taxonomy.resolve_moods` produit
les mêmes `GenreStyle` que `resolve`, avec « Ambiance » pour groupe et l'axe
marqué. Seuils, repli, nommage, diff et application opèrent ensuite
indifféremment sur l'un ou l'autre axe — seule la présentation distingue
« GENRE / STYLE » de « AMBIANCE ».

Une release sans style exploitable ne relève d'aucune ambiance et n'est rangée
nulle part en ce mode : un genre seul ne dit rien de l'humeur, « Rock »
recouvrant aussi bien Shoegaze que Grindcore.

### 16. Le titre, et non le disque

Discogs étiquette des *releases*. `query_key` regroupant par (artiste, album),
les douze titres d'un disque reçoivent exactement les mêmes styles — une
ballade sur un album punk est donc classée « Punk », puis « Énergique » en mode
ambiance. C'est la cause unique des deux défauts observés à l'usage : des
titres mal placés, et des playlists trop nombreuses, un album à deux styles
plaçant *toutes* ses pistes dans les deux.

Les tags Last.fm corrigent la cause, étant posés sur le morceau. Ils
interviennent à deux endroits :

* **le style** — un tag nommant un style connu remplace ceux de la release.
  Seuls ces tags-là sont retenus, ce qui écarte le bruit (« 00s »,
  « seen live ») sans liste noire à tenir, et un poids minimal évite qu'un tag
  posé par trois auditeurs ne fasse loi ;
* **l'ambiance** — une table `tag_moods` traduit les tags d'humeur explicites,
  et prime sur la déduction par style, qui n'est plus qu'un repli. Les poids
  **se cumulent** par ambiance : « sad », « melancholy » et « melancholic »
  disent la même chose et doivent s'additionner plutôt que se concurrencer.

Les deux seuils n'ont rien à voir l'un avec l'autre, et c'est l'observation de
données réelles qui l'a montré. Last.fm normalise à 100 le tag le plus posé, or
on étiquette bien plus volontiers un genre qu'une humeur : sur *Something In
The Way*, « Grunge » pèse 100 quand « acoustic » pèse 5 et « sad » pèse 1.
Appliquer aux humeurs le seuil des styles les éliminerait donc toutes. D'où un
`min_mood_weight` bas et distinct — et un cumul, car se fier à l'ordre
d'apparition laissait un tag pondéré à 1 décider de l'ambiance d'un morceau.

L'ambiance est arrêtée au classement, seul endroit où les poids sont connus, et
rangée avec la classification ; le planner n'a plus qu'à la lire.

### 17. Le plafond des métadonnées, et ce qui le dépasse

Les tags Last.fm ont déplacé le problème sans le résoudre. Ils décrivent bien le
titre, mais ce sont des étiquettes posées par des gens qui nomment volontiers un
genre et rarement une humeur. Sur *Something In The Way*, une fois les poids
relevés : `grunge 100`, `acoustic 5`, `sad 1`. Le style retenu reste « Grunge »
pour une berceuse. Aucun réglage de seuil ne fait dire à ces données ce
qu'elles ne contiennent pas — deux tentatives de resserrage l'ont établi.

La seule source qui connaisse la *musique* plutôt que ses étiquettes est un
modèle. `enrich` lui soumet chaque titre avec ce que les bases en disent — à
titre d'indice, explicitement, puisque c'est leur imprécision qui motive la
passe — et en obtient un genre, un style, une ambiance et une justification,
pour le morceau lui-même.

Quatre contraintes ont façonné cette partie, toutes tenant à ce qu'elle est la
seule du projet à coûter de l'argent.

**Ne jamais juger deux fois.** Le résultat est écrit en clair dans
`data/verdicts.txt`, une ligne par titre, indexée par (artiste, titre)
normalisés — jamais par `video_id`, pour que le fichier survive à un
changement de compte, et jamais par album, pour que deux éditions d'un morceau
partagent leur verdict. Ce fichier fait autorité : il est consulté avant tout
appel, et sa ligne l'emporte sur Discogs comme sur Last.fm au moment de
classer. Une base effacée ne coûte donc rien à reconstituer.

Cette clé a dû être reprise après coup, sur deux défauts qu'aucun test ne
couvrait. Un titre à plusieurs artistes partait vers le modèle sous « Daft Punk,
Pharrell Williams » et était cherché sous « Daft Punk » : son verdict n'était
jamais appliqué, et le titre repartait — et se repayait — à chaque passe. Et un
même morceau publié comme clip, sur une chaîne « Topic » ou « VEVO », ou avec
« (Official HD Video) », passait pour autant de morceaux distincts. `song_key`
règle les deux : artiste principal seul, suffixes de chaîne retirés, préfixe
« Artiste - » ôté du titre quand il nomme bien l'artiste, et bruit éditorial
retiré jusqu'à ses reliquats (« official », « version »). Un segment qui dit
quelque chose de la musique — live, remix, acoustique — arrête le nettoyage :
ces versions-là n'ont pas forcément l'ambiance de l'originale. `ytmgc doublons`
expose ce regroupement, pour qu'une variante qui lui échapperait se signale.

**Annoncer avant de dépenser.** `estimate` chiffre la passe — requêtes, jetons,
dollars — à partir du format réel du prompt, volontairement pessimiste du côté
sortie puisque la réflexion du modèle s'y facture. Le montant est affiché dans
le terminal comme dans l'interface, et la confirmation demandée le porte :
« oui » doit vouloir dire « oui, ce prix-là ». `--dry-run` s'arrête à
l'annonce, sans même exiger de clé.

**Survivre à une coupure.** Le dépôt et la récupération sont deux temps
distincts. L'identifiant du lot et sa découpe en requêtes sont écrits sur le
disque *aussitôt* le dépôt fait : sans la découpe, un verdict ne saurait plus à
quel titre il se rapporte, et un lot payé serait perdu. Le lot vit ensuite chez
Anthropic — l'ordinateur peut s'éteindre, `--resume` va le chercher, et
l'interface propose de le reprendre au lieu d'en déposer un second.

**Constater avant d'engager.** La qualité d'un classement ne se promet pas.
`--essai` juge quelques dizaines de titres pour une poignée de centimes, et
l'interface propose trois étendues — essai, titres non rangés, bibliothèque
entière — chacune portant *son* montant, l'essai coché par défaut. Un essai
n'est pas une dépense perdue : ses verdicts entrent dans le fichier comme les
autres et ne sont pas redemandés par la passe suivante.

**Laisser le dernier mot à l'utilisateur.** Une ligne dont la colonne source
porte `manuel` n'est jamais écrasée par le modèle, et le fichier est conçu pour
être ouvert : en-tête explicatif, colonnes séparées par des tabulations,
lecture tolérante aux lignes bancales, écriture par fichier temporaire puis
renommage atomique. Corriger un classement, c'est éditer une ligne. Ce fichier est d'ailleurs le
seul contenu de `data/` que le dépôt versionne : le reste se reconstruit
gratuitement, lui non.

Deux choix de forme découlent du reste. Le vocabulaire — 15 genres, 199 styles,
8 ambiances — est transmis en préambule et mis en cache, puisqu'il est
identique d'une requête à l'autre et représenterait sinon l'essentiel de la
facture. Genre et ambiance sont **contraints** par le schéma de sortie : une
ambiance libre rendrait ce tri inutilisable, qui est un ensemble fermé de huit
playlists. Le style, lui, reste ouvert et seulement *orienté* vers la liste
connue : forcer un morceau dans un terme qui ne lui va pas serait revenir
exactement au défaut qu'on corrige.

Enfin `Classification.judged` marque un titre tranché par le modèle. `status`
ne parle que de Discogs et continue de ne parler que de lui ; un titre jugé
devient rangeable même quand Discogs n'a rien su en dire, ce que le planner lit
directement. Un verdict peu assuré, lui, ne détruit rien : le modèle a dit
qu'il ne connaissait pas le morceau, et on le croit.

En mode ambiance, un titre que Discogs n'a pas su apparier reste classable dès
lors que ses auditeurs l'ont décrit : la couverture s'en trouve élargie, non
réduite.

Le cache suit la même logique que celui de Discogs, mais indexé par (artiste,
titre) — deux morceaux d'un même disque n'ont aucune raison de partager leurs
tags. L'absence de tags est mise en cache comme le reste : c'est un résultat,
pas une panne, et il ne sert à rien de le redemander.

## Contraintes des API

| Contrainte | Conséquence |
|---|---|
| Discogs : 60 req/min authentifié | Seau de jetons (`ratelimit.py`) + cache par album. |
| Discogs : 429 et 5xx | Reprises avec repli exponentiel, `Retry-After` respecté. |
| YouTube Music : pas de dossier | Hiérarchie encodée dans le nom. |
| YouTube Music : 5 000 titres/playlist | Les seuils de repli maintiennent les playlists loin du plafond. |
| YouTube Music : suppression par `setVideoId` | `sync` relit la playlist avant tout retrait. |
| API Claude : chaque appel se facture | Fichier de verdicts consulté avant tout appel, montant annoncé avant toute dépense, confirmation explicite, lot déposé jamais redéposé. Une clé absente désactive proprement la fonctionnalité. |
| API Claude : un lot peut prendre 24 h | Dépôt et récupération séparés ; l'identifiant et la découpe sont écrits sur le disque, le lot vit chez Anthropic et ses résultats y restent 29 jours. |
| Last.fm : une requête par titre, débit limité | Cache en base par (artiste, titre), TTL de 180 jours. La passe ne se paie qu'une fois ; une clé absente désactive proprement la fonctionnalité. |
| YouTube Music : pas de décompte dans la liste des playlists | Le champ `count` de `ytmusicapi` est le premier mot d'un sous-titre — « 2 » pour « 2 188 titres », un mot quelconque selon la langue. Il est ignoré : chaque source est mesurée par `count_source`, qui lit le `trackCount` d'une playlist en un appel et ne parcourt réellement que la bibliothèque et les mises en ligne, faute d'un total annoncé. |
| YouTube Music : API interne, non officielle | Toute la dépendance est isolée dans un seul adaptateur, importé paresseusement. |
| YouTube Music : session par cookies de navigateur | Le fichier d'authentification expire au bout de quelques semaines et se renouvelle depuis l'interface. Une session utilisée depuis une IP de centre de données déclenche plus facilement un contrôle Google : le Codespace convient à un usage ponctuel, moins à un service permanent. |

### 18. La même passe, sans API : la bibliothèque exportée pour Claude.ai

La passe modèle n'a pas besoin de l'API pour exister : seul son transport en
dépend. `chat.py` exporte tous les titres à juger, d'un coup, en fichiers
autonomes — consignes, vocabulaire, titres — que l'utilisateur glisse chacun
dans une conversation Claude.ai, puis relit les réponses recollées. Les règles
de jugement sont *les mêmes chaînes* que celles de la passe payante (`INTRO`,
`GUIDANCE`, `RULES`), et le fichier de verdicts, la clé de morceau et la
préséance de la décision humaine sont partagés.

**Plusieurs fichiers, pas un.** Une conversation garde en mémoire les titres
et toutes ses réponses. Pour ~4 000 titres, environ 220 000 jetons : au-delà
de ce qu'elle tient. Des fichiers de 500 (`claude.export_size`) restent vers
30 000.

**Une première version, par paquets numérotés, s'est révélée illisible à
l'usage** : on ne savait ni où allait le résultat, ni ce qui était fait, ni à
quoi il ressemblait, et la réponse devait être recollée face au bon paquet. La
version actuelle supprime tout état côté conversation. Claude répond une ligne
par morceau au format même du fichier de verdicts — `artiste | titre | genre |
style | ambiance | confiance | note` — et **c'est la ligne qui porte l'identité
de son morceau**. Une réponse se recolle dans n'importe quel ordre, en autant
de fois qu'on veut, des jours plus tard.

**La lecture reste défiante**, car rien ne contraint la forme d'une réponse de
conversation et le pire défaut n'est pas le verdict manquant mais le verdict
mal rattaché, tenu ensuite pour acquis :

* une ligne doit désigner un morceau de la bibliothèque, via `song_key` —
  tolérante sur la forme (casse, bruit éditorial du titre, invité ajouté à
  l'artiste), jamais sur le fond : un autre morceau ne correspond pas ;
* genre et ambiance doivent appartenir au vocabulaire fermé, à la casse et
  aux accents près — une ambiance inventée ouvrirait une neuvième playlist ;
* le verdict est écrit sous l'orthographe *de la bibliothèque*, non de la
  réponse, pour être retrouvé.

Ce qui est écarté s'affiche avec sa raison ; le morceau reste à juger et
revient au prochain export.

**Le suivi est visible en permanence.** `progress.py` sert le bloc *Résultats*
de l'interface et `ytmgc suivi` : nombre de morceaux jugés, chemin complet du
fichier de verdicts, et chaque morceau avec son verdict ou son absence. Le
manifeste de l'export (`export.json`) donne l'avancement de chaque fichier.
Les fichiers exportés ne sont servis que par leur nom, validé contre leur
motif : aucun autre fichier du disque n'est atteignable par l'interface.

### 19. Un plan de playlists écrit à l'avance, plutôt que déduit

Une fois la bibliothèque réelle jugée (4 083 verdicts), le tri par seuils
restait mauvais, et pour des raisons mesurables :

* **293 couples genre/style distincts**, soit environ 120 playlists en mode
  Détaillé — le style étant libre, Claude nomme finement ;
* **un même style éparpillé entre genres** : Bossa Nova sous Jazz, Latin, Pop,
  Folk et BO ; Contemporary R&B sous quatre genres ; « Ballad », qui décrit une
  forme et non un style, sous sept ;
* **des tailles extrêmes** : Deep House 404 titres, ambiance Groovy 1 281 ;
* **un axe à la fois** : « Calme » mêlait Chopin, rap jazzy et bossa, alors
  que les gros styles se découpent bien par ambiance (Trap = 103 énergiques,
  64 sombres, 62 mélancoliques) ;
* **un verdict sur cinq écarté** par le seuil de confiance de 0,35. Claude.ai
  s'en servait pour dire « je connais mal ce titre », pas « mon classement est
  douteux » : ses verdicts peu sûrs restaient plausibles, et en les écartant on
  retombait sur Discogs, qui juge l'album.

Aucun réglage de seuil ne corrige cela : le défaut est de *déduire* les
playlists. `playlist_plan.py` inverse la démarche. `config/playlists.toml`
énumère les playlists voulues ; chaque bloc `[[playlist]]` porte une règle sur
genres, styles et ambiances, et **la première règle qui correspond
l'emporte**. Les choix qui en découlent :

* **L'ordre tranche les cas limites.** Les playlists transversales (Brésil,
  R&B, variété) viennent d'abord et réunissent un style quel que soit le genre
  du verdict ; chaque famille suit, de la règle étroite à la large.
* **Pas de fourre-tout implicite.** Un morceau qu'aucune règle n'accepte est
  un non-rangé de cause `no_rule`, affiché avec son genre, son style et son
  ambiance. Sur la bibliothèque réelle : 4 sur 4 083 — un sketch, un pack
  d'échantillons, une annonce et une comptine.
* **Le fichier est vérifié, pas deviné.** Genres et ambiances doivent
  appartenir au vocabulaire fermé, les clés inconnues sont refusées : une
  règle mal orthographiée ne correspondrait à rien, en silence.
* **La taille est une alerte, pas une contrainte.** `[taille]` borne la plage
  visée ; l'aperçu signale ce qui en sort, mais ne redécoupe rien de lui-même.
* **Tous les verdicts s'appliquent** (`claude.min_confidence = 0`). Les peu sûrs
  (`unsure_below`) sont marqués « à vérifier » dans l'aperçu, avec la note de
  Claude.
* **L'aperçu relit le fichier de verdicts** et le reporte en base avant de
  planifier : une correction manuelle se voit au calcul suivant, sans nouvelle
  analyse.

La clé d'une playlist du plan est `plan/` suivi de son nom : renommer une
playlist en crée une autre, et l'ancienne devient sans objet.

**Une famille réelle, une ambiance constante.** Une première version rangeait
sous « Monde » tout ce qui n'était ni jazz, ni rap, ni électro. Le mot ne dit
rien de ce qu'on va entendre, et chaque playlist « Monde · région » sautait
d'une ambiance à l'autre : Mulatu Astatke à côté de Tyla, les chants des
oasis à côté du funk libyen. Le plan range donc chaque morceau dans une
famille musicale réelle — Brésil, Maghreb, Orient, Afrique, Caraïbes, Latino,
Folk, ou une famille existante quand elle convient (ethio-jazz et latin jazz
dans Jazz, afrobeat et funk arabe des années 70 dans Funk) — puis découpe
chaque famille jusqu'à ce que l'ambiance tienne : la plupart en une playlist
vive et une douce, certaines en trois (le boom bap groovy, dur, mélancolique).
Le découpage s'écrit en deux blocs — le premier nomme les ambiances, le second,
identique sans ambiance, prend le reste —, si bien qu'un titre à l'ambiance
inconnue garde sa famille. Mesuré sur la bibliothèque réelle, l'ambiance
dominante couvre désormais 60 à 100 % de presque chaque playlist, contre
souvent 30 % auparavant ; les « Divers » ont disparu. Le seuil bas de `[taille]`
(5) en découle : une playlist de six titres de flamenco vaut mieux qu'une de
vingt qui finit sur un chœur russe.

**Les artistes, pour ranger par région.** La musique du monde se découpait
mal : le verdict dit « Ballad », « Traditional » ou « Instrumental » pour
Fairouz comme pour les compagnies des oasis marocaines. Une règle peut donc
nommer des artistes (`artistes = [...]`), comparés comme les clés de morceau
— casse, accents, chaîne « Topic » ou « VEVO » indifférents — et sur tous les
artistes du titre, invités compris. Une liste d'artistes vieillit mal : ceux
qu'aucun titre de la bibliothèque ne porte sont donc signalés à l'aperçu.

**Pas de « BO ».** « Stage & Screen » dit l'usage d'une musique, pas ce
qu'elle est : une playlist BO mêlait Zimmer à l'orgue, Miles Davis et des
reprises de Mario. Le genre n'est plus proposé au modèle (il sort de
`genre_priority`) ; une réponse qui l'emploie est refusée, et les 57 verdicts
existants ont été rejugés selon leur musique.

### 20. Un déplacement est une décision, pas un réglage d'affichage

*Mise à jour sur place.* Recalculer l'aperçu après chaque déplacement
relisait le compte YouTube Music et renvoyait des milliers de titres : une
attente à chaque clic. Le serveur renvoie désormais, pour chaque titre
déplacé, sa nouvelle place et sa fiche ; la page retire le titre de son
ancienne playlist, l'ajoute à la nouvelle et recompte, sans autre requête.
Les compteurs « +N / −N » face au compte ne sont exacts qu'au prochain
« Recalculer l'aperçu ».

*Les playlists voisines.* Chaque titre propose jusqu'à trois autres
playlists, en boutons sous son nom. Les règles disent où un titre *doit*
aller, pas où il pourrait aussi aller ; ce que chaque playlist contient le
dit mieux. `suggest_playlists` pondère la part des autres titres du même
artiste qui s'y trouvent, puis le style, le genre et l'ambiance. Il faut un
lien d'artiste, de style ou de genre : l'ambiance seule rapprochait
Interstellar d'une transe gnaoua. Le pays connu d'un artiste écarte les
playlists d'autres pays, et la playlist d'attente (`pays = ["?"]`).

*Créer une playlist depuis l'aperçu.* `add_manual_playlist` ajoute un bloc
`manuelle = true` en fin de `config/playlists.toml`, sous un en-tête dédié. Le
fichier est complété, jamais réécrit : ses commentaires et l'ordre de ses
règles restent intacts, et le résultat est relu avant d'être enregistré. Une
playlist manuelle n'a pas de règle : elle ne prend que les titres déplacés
vers elle, ce qui rend sa place dans le fichier sans effet. Elle ne peut pas
porter de critère — elle ressemblerait alors aux autres sans le dire.

*Choisir où déplacer.* Le menu déroulant listait quatre-vingts noms dans
l'ordre du plan : illisible, et sans recherche. Le bouton « Déplacer » ouvre
désormais une fenêtre où les playlists sont regroupées par famille — ce qui
précède « · » dans leur nom —, familles et playlists par ordre alphabétique,
les noms sans famille dans « Autres » à la fin. Un champ filtre à la frappe,
sans tenir compte des accents ni de la casse, sur le nom entier : taper
« funk » montre toute la famille Funk et Jazz · Jazz-funk & groove. Entrée
prend la première playlist trouvée ; sans résultat, elle ouvre la création
d'une playlist au nom tapé. Chaque famille est un bloc à en-tête teinté,
et chaque playlist montre son nombre de titres, lu dans l'aperçu tel que
les déplacements l'ont mis à jour : on voit si on remplit une playlist ou si
on en commence une.

*Retirer.* Le ✕ d'un titre est un déplacement vers « (aucune) » : pas de
nouveau mécanisme, et le même retour en arrière (« Rendre aux règles »). Il
ne touche pas à la bibliothèque YouTube Music.

*Le rap par pays.* À la demande de l'utilisateur, le rap se découpe d'abord
par pays : Rap · FR, US, UK, Maghreb, Ailleurs. Le rap francophone (582 titres,
toutes ambiances mêlées) est ensuite redécoupé par ambiance — énergique,
sombre, groovy & posé, mélancolique — ; US et UK restent d'un seul tenant.
Pour un titre dont le pays est inconnu, les suggestions proposent une
playlist par pays plutôt que trois variantes du rap français. Le pays est
une propriété de l'artiste, tenue dans `data/pays.txt` et désignée par une
règle `pays`. Les 258 artistes identifiés avec assurance y figurent ; les
autres attendent dans « Rap · Pays à préciser », plutôt que d'être devinés.

Décocher un titre dans l'aperçu ne vaut que pour l'application en cours. Le
*déplacer* est d'une autre nature : c'est dire où il doit vivre, une fois pour
toutes. `placements.py` le tient donc hors de la base, dans
`data/placements.txt`, versionné, par morceau (`song_key`) et non par
identifiant vidéo — une analyse vide la base, et un morceau a plusieurs
publications.

* **Le déplacement l'emporte sur les règles**, y compris pour un morceau que
  les règles ne savent pas ranger (un sketch, un titre sans verdict). « (aucune) »
  le tient hors de toute playlist : cause `kept_out` dans les non-rangés.
* **La cible est désignée par son nom.** Lisible dans le fichier, mais fragile
  si le plan change : un déplacement vers une playlist disparue est ignoré —
  le morceau suit les règles — et nommé dans l'aperçu, jamais perdu en silence.
* **Seul le tri « Par famille » l'applique** : les autres tris n'ont pas de
  playlists fixes où déplacer.
* **L'ordre de la relecture est figé.** Les artistes sont classés par nombre
  de titres à leur première apparition, puis gardent leur place — mémorisée
  dans le navigateur. Trié à chaque rendu, valider deux titres d'un artiste le
  faisait passer derrière le suivant : la liste bougeait sous les yeux.
* **La relecture des verdicts peu sûrs est regroupée par artiste.** Un artiste
  inconnu du modèle l'est pour tous ses titres, et se corrige d'un geste.
  Valider un verdict le fait passer en source `manuel` : c'est désormais une
  décision, qui n'est ni réécrite par le modèle ni redemandée à la relecture.
  Un titre déplacé sort aussi de la relecture : sa place a été décidée.
* **L'aperçu se recalcule après chaque déplacement**, en conservant ce que
  l'utilisateur a ouvert et décoché : sans cela, chaque déplacement refermerait
  la playlist en cours de relecture. Le gestionnaire est lié une seule fois,
  l'aperçu étant reconstruit à chaque calcul.

### 21. Relire des playlists plutôt que des titres

Vérifier un par un les ~800 titres au verdict peu sûr s'est révélé
intenable : des heures de clics, dont la moitié confirmait un titre déjà bien
rangé (sur les 24 premières décisions, 12 validations pour 12
déplacements). Et la question posée était la mauvaise : un titre jugé seul ne
dit pas s'il garde la vibe de sa playlist.

La relecture exporte donc les *playlists* entières — nom, description, tous
les titres — en quelques fichiers pour Claude.ai, chacun avec la liste de
toutes les playlists du plan et leur description. Claude ne répond que pour
les intrus : `artiste | titre | playlist proposée | raison`. Repérer ce qui
détonne dans une liste est exactement ce qu'un modèle fait bien, et ce
qu'aucune règle sur le genre ne capture.

* **Une proposition n'est qu'un avis.** Elle attend dans
  `data/propositions.txt` ; « Accepter » en fait un déplacement, « Autre
  playlist » ouvre le choix habituel quand ni la proposée ni l'actuelle ne
  conviennent, « Garder ici » valide le verdict du titre. Rien ne bouge sans un clic.
* **Ce qui a été décidé à la main n'est jamais proposé** — titre déplacé ou
  verdict validé. Il figure dans les fichiers, marqué `[validé]`, parce qu'il
  dit ce qu'est la playlist. Un déplacement fait à la main tranche aussi la
  proposition du titre.
* **Les familles voyagent ensemble.** Les fichiers (600 titres au plus) ne
  coupent jamais une playlist et regroupent les playlists d'une même famille :
  Claude juge mieux « Jazz · Bop » quand « Jazz · Fusion » est sous ses yeux.
* **La réponse est lue avec défiance**, comme celle des verdicts : un titre
  introuvable ou une playlist hors du plan sont écartés avec leur raison. Les
  noms de playlist sont tolérés sans accents ni casse, ou sans la famille
  quand le nom est unique.
* **« Tout valider » clôt la vérification titre par titre.** Un verdict peu
  sûr que la relecture a vu dans sa playlist sans le signaler n'a plus de
  raison d'être vérifié seul ; le bouton les valide tous, après une
  confirmation explicite, sauf ceux qu'une proposition attend encore.
* **Le fichier répondu est noté** grâce à la ligne `FICHIER n SUR m` que la
  réponse recopie ; `RIEN` est une réponse valide.

## Tests

707 tests, aucun appel réseau, y compris l'API web complète (aperçu,
application, annulation), son contrôle d'accès et les quatre voies de connexion.

Quatre-vingt-onze d’entre eux chargent l’interface dans un vrai navigateur (`tests/test_ui.py`).
Le câblage du DOM échappe aux tests Python : deux défauts d'onglets sont passés
au travers de la suite avant d'être vus à l'écran. Ces tests sont ignorés
lorsque Playwright ou son navigateur sont absents, pour que la suite reste
exécutable avec les seules dépendances de base. Les adaptateurs externes sont doublés en mémoire
(`tests/fakes.py`), y compris pour un test de bout en bout scan → classify →
plan → apply qui vérifie l'idempotence du second run et l'intégrité des
playlists manuelles.
