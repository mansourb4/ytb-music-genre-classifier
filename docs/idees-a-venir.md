# Idées à venir

Notées pour plus tard, rien n'est encore construit.

## Ajouter de nouveaux morceaux depuis le téléphone

- **Geste** : ajouter le titre à la bibliothèque YouTube Music, depuis l'appli.
- **`ytmgc nouveautes`** : une seule commande qui, pour les seuls titres
  nouveaux, enchaîne analyse, classement (Discogs, Last.fm, jugement par le
  modèle si la clé est là), playlists YouTube Music, téléchargement (256 kbps,
  genre) et ajout dans la bonne playlist d'Apple Music.
- **Planification** : lancée chaque nuit par launchd, Mac branché ; réveil
  programmable avec `pmset`.
- **Vérification depuis le téléphone** : `ytmgc web --host 0.0.0.0`, en Wi-Fi
  à la maison, pour déplacer un titre mal rangé ; appliqué partout la nuit
  suivante.
- **Écoute sur l'iPhone**, sans abonnement Apple Music : synchronisation
  Finder par Wi-Fi (gratuite, iPhone en charge sur le même réseau) ou
  iTunes Match (environ 25 €/an, via iCloud).

## Préalable : script Apple Music

Créer dans Apple Music les dossiers (Électro, Jazz…) et les playlists, et y
ranger les fichiers de `~/Music/Playlists`, en pouvant le relancer quand un
classement change.
