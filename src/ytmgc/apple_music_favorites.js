// Met en favoris (« J'adore ») des titres de la bibliothèque Apple Music.
//
// Lancé par `ytmgc apple-music-likes` :
//   osascript -l JavaScript apple_music_favorites.js ENTRÉE
// ENTRÉE est un JSON { pids: [persistentID…], dryRun } ; la sortie (stdout),
// un JSON { favorited, already, gone }. Ne retire jamais un favori.

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

function setFavorite(track) {
  try {
    track.favorited = true;
  } catch (error) {
    track.loved = true;
  }
}

function run(argv) {
  const input = readJSON(argv[0]);
  const Music = Application('Music');
  const tracks = Music.libraryPlaylists[0].fileTracks;
  const report = { favorited: 0, already: 0, gone: 0 };

  console.log('Mise en favoris de ' + input.pids.length + ' titre(s)…');
  input.pids.forEach(function (pid, index) {
    const found = tracks.whose({ persistentID: pid });
    if (!found.length) {
      report.gone++;
      return;
    }
    const track = found[0];
    if (isFavorite(track)) {
      report.already++;
    } else {
      if (!input.dryRun) setFavorite(track);
      report.favorited++;
    }
    if ((index + 1) % 50 === 0) console.log('  ' + (index + 1) + '/' + input.pids.length);
  });
  return JSON.stringify(report);
}
