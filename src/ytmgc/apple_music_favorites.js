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
// `reorder` retire d'abord tous ces favoris, puis les remet un à un dans
// l'ordre ; sans lui, seuls les nouveaux favoris sont ajoutés. Seuls les
// titres de `pids` sont touchés.

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

  // Titres présents, du like le plus ancien au plus récent.
  const ordered = [];
  input.pids.slice().reverse().forEach(function (pid) {
    const found = tracks.whose({ persistentID: pid });
    if (found.length) ordered.push(found[0]);
    else report.gone++;
  });

  if (input.reorder) {
    // Tous retirés d'abord, en une passe : remettre chacun aussitôt après
    // l'avoir retiré ne suffit pas à Music pour changer sa place.
    console.log('Retrait des favoris : ' + ordered.length + ' titre(s)…');
    ordered.forEach(function (track) {
      if (!input.dryRun && isFavorite(track)) setFavorite(track, false);
    });
    if (!input.dryRun && input.interval > 0) delay(Math.max(3, input.interval));
  }

  console.log('Favoris, du like le plus ancien au plus récent : ' + ordered.length +
    ' titre(s)…');
  let done = 0;
  ordered.forEach(function (track, index) {
    if (!input.reorder && isFavorite(track)) {
      report.already++;
    } else {
      if (!input.dryRun) {
        if (done > 0 && input.interval > 0) delay(input.interval);
        setFavorite(track, true);
      }
      done++;
      report.favorited++;
    }
    if ((index + 1) % 50 === 0) console.log('  ' + (index + 1) + '/' + ordered.length);
  });
  return JSON.stringify(report);
}
