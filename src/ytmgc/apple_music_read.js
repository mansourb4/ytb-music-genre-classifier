// Photographie Apple Music, sans rien y modifier : titres de la bibliothèque,
// favoris, et playlists rangées dans un dossier.
//
// Lancé par `ytmgc synchro` : osascript -l JavaScript apple_music_read.js ENTRÉE
// ENTRÉE est un JSON {} (réservé) ; la sortie (stdout), un JSON
//   { tracks: [persistentID…], favorites: [persistentID…],
//     playlists: [{pid, name, folder, tracks: [persistentID…]}] }

ObjC.import('Foundation');

// Propriété de tous les éléments d'une collection en un échange ; Music
// refuse la demande groupée sur une collection vide, d'où le compte d'abord.
function every(items, property) {
  const count = items.length;
  if (!count) return [];
  try {
    return items[property]();
  } catch (error) {
    const values = [];
    for (let i = 0; i < count; i++) {
      try {
        values.push(items[i][property]());
      } catch (inner) {
        values.push(null);
      }
    }
    return values;
  }
}

function run(argv) {
  const Music = Application('Music');
  const library = Music.libraryPlaylists[0].fileTracks;
  console.log('Lecture d’Apple Music…');

  const tracks = every(library, 'persistentID');
  // « favorited » depuis macOS 14, « loved » avant.
  let flags;
  try {
    flags = every(library, 'favorited');
    if (flags.length && flags.every(function (f) { return f === null; })) throw new Error();
  } catch (error) {
    flags = every(library, 'loved');
  }
  const favorites = tracks.filter(function (pid, i) { return flags[i] === true; });

  const all = Music.playlists;
  const classes = every(all, 'class');
  const names = every(all, 'name');
  const ids = every(all, 'persistentID');
  const folderNames = {};
  for (let i = 0; i < ids.length; i++) {
    if (classes[i] === 'folderPlaylist') folderNames[ids[i]] = names[i];
  }
  const playlists = [];
  for (let i = 0; i < ids.length; i++) {
    if (classes[i] !== 'userPlaylist') continue;
    let folder = null;
    try {
      folder = folderNames[all[i].parent().persistentID()] || null;
    } catch (error) {
      continue;
    }
    if (!folder) continue;
    playlists.push({
      pid: ids[i], name: names[i], folder: folder,
      tracks: every(all[i].tracks, 'persistentID'),
    });
  }
  return JSON.stringify({ tracks: tracks, favorites: favorites, playlists: playlists });
}
