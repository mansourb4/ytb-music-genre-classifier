// Met en favoris (« J'adore ») des titres de la bibliothèque Apple Music.
//
// Lancé par `ytmgc apple-music-likes` :
//   osascript -l JavaScript apple_music_favorites.js ENTRÉE
// ENTRÉE est un JSON { pids: [persistentID…], reorder, interval, dryRun } ;
// la sortie (stdout), un JSON { favorited, already, gone }.
//
// `pids` est dans l'ordre des likes de YouTube Music, du plus récent au plus
// ancien. Apple Music présente ses favoris du plus récemment ajouté au plus
// ancien : on les ajoute donc en partant de la fin, le plus récent en dernier,
// avec `interval` secondes entre deux pour que leurs dates se suivent.
// `reorder` retire puis remet un favori déjà là, pour le replacer dans
// l'ordre ; sans lui, seuls les nouveaux favoris sont ajoutés. Seuls les titres
// de `pids` sont touchés.

ObjC.import('Foundation');

function readJSON(path) {
  const text = $.NSString.stringWithContentsOfFileEncodingError(
    path, $.NSUTF8StringEncoding, null);
  return JSON.parse(ObjC.unwrap(text));
}

// La propriété s'appelle « favorited » depuis macOS 14, « loved » avant.
function isFavorite(track) {
  try {
    return track.favorited();
  } catch (error) {
    return track.loved();
  }
}

function setFavorite(track, value) {
  try {
    track.favorited = value;
  } catch (error) {
    track.loved = value;
  }
}

function run(argv) {
  const input = readJSON(argv[0]);
  const Music = Application('Music');
  const tracks = Music.libraryPlaylists[0].fileTracks;
  const report = { favorited: 0, already: 0, gone: 0 };
  const ordered = input.pids.slice().reverse();
  let done = 0;

  console.log('Favoris, du like le plus ancien au plus récent : ' + ordered.length +
    ' titre(s)…');
  ordered.forEach(function (pid, index) {
    const found = tracks.whose({ persistentID: pid });
    if (!found.length) {
      report.gone++;
      return;
    }
    const track = found[0];
    const already = isFavorite(track);
    if (already && !input.reorder) {
      report.already++;
    } else {
      if (!input.dryRun) {
        if (done > 0 && input.interval > 0) delay(input.interval);
        if (already) setFavorite(track, false);
        setFavorite(track, true);
      }
      done++;
      report.favorited++;
    }
    if ((index + 1) % 50 === 0) console.log('  ' + (index + 1) + '/' + ordered.length);
  });
  return JSON.stringify(report);
}
