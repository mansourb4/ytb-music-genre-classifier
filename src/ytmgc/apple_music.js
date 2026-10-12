// Range les fichiers de ~/Music/Playlists dans Apple Music : un dossier par
// famille, une playlist par nom, chaque titre dans ses playlists.
//
// Lancé par `ytmgc apple-music` : osascript -l JavaScript apple_music.js ENTRÉE
// ENTRÉE est un fichier JSON écrit par l'outil :
//   { root, known: {videoId: persistentID}, items: [{id, path, genre, grouping}],
//     playlists: [{family, name, ids: [videoId…]}], families: [nom…], dryRun }
// La sortie (stdout) est un JSON : identifiants retenus et compte rendu.
// L'avancement s'écrit sur stderr, une ligne par étape.
//
// Ne touche qu'aux playlists rangées dans les dossiers des familles du plan ;
// ne supprime jamais un titre de la bibliothèque, ni une playlist.

ObjC.import('Foundation');

function readJSON(path) {
  const text = $.NSString.stringWithContentsOfFileEncodingError(
    path, $.NSUTF8StringEncoding, null);
  return JSON.parse(ObjC.unwrap(text));
}

function log(message) {
  console.log(message);
}

// macOS rend les chemins décomposés (« E » + accent) : comparés recomposés.
function nfc(text) {
  return text ? String(text).normalize('NFC') : null;
}

const CHUNK = 50;

// Propriété de tous les éléments d'une collection, en un seul échange avec
// Music. Une collection vide fait échouer la demande groupée (« impossible
// d'obtenir l'objet ») : on compte d'abord. Si la demande groupée échoue
// quand même (un élément illisible), on lit élément par élément.
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

// Création d'une playlist ou d'un dossier, sous les deux écritures que JXA
// accepte selon les versions de macOS.
function makePlaylist(Music, kind, name) {
  try {
    return Music.make({ new: kind, withProperties: { name: name } });
  } catch (error) {
    const made = (kind === 'folderPlaylist' ? Music.FolderPlaylist : Music.UserPlaylist)({ name: name });
    made.make();
    return made;
  }
}

function run(argv) {
  const input = readJSON(argv[0]);
  const Music = Application('Music');
  const library = Music.libraryPlaylists[0];
  const report = {
    known: {}, added: 0, relocated: 0, retagged: 0, missing: [],
    createdFolders: [], createdPlaylists: [], trackAdds: 0, trackRemovals: 0,
    stale: [], copied: false, copiedTo: null,
  };

  // ------------------------------------------------------- bibliothèque

  function inventory() {
    const tracks = library.fileTracks;
    const pids = every(tracks, 'persistentID');
    const locations = every(tracks, 'location');
    const genres = every(tracks, 'genre');
    const groupings = every(tracks, 'grouping');
    const byPid = {};
    const byPath = {};
    for (let i = 0; i < pids.length; i++) {
      const path = locations[i] ? nfc(locations[i].toString()) : null;
      byPid[pids[i]] = { index: i, path: path, genre: genres[i], grouping: groupings[i] };
      if (path) byPath[path] = pids[i];
    }
    return { byPid: byPid, byPath: byPath };
  }

  log('Lecture de la bibliothèque Apple Music…');
  let inv = inventory();
  const pidOf = {};
  const toAdd = [];

  for (const item of input.items) {
    item.path = nfc(item.path);
    let pid = input.known[item.id];
    if (pid && !inv.byPid[pid]) pid = null;
    if (!pid && inv.byPath[item.path]) pid = inv.byPath[item.path];
    if (pid) {
      pidOf[item.id] = pid;
      if (inv.byPid[pid].path !== item.path) {
        // Fichier déplacé depuis la dernière fois (titre changé de playlist).
        if (!input.dryRun) {
          library.fileTracks[inv.byPid[pid].index].location = Path(item.path);
        }
        report.relocated++;
      }
    } else {
      toAdd.push(item);
    }
  }

  // ------------------------------------------------------------- ajouts

  if (toAdd.length && !input.dryRun) {
    log('Ajout de ' + toAdd.length + ' fichier(s) à la bibliothèque…');
    // Un premier fichier seul : si Apple Music le copie dans son propre
    // dossier, on s'arrête avant d'en dupliquer des milliers.
    const probe = Music.add(Path(toAdd[0].path));
    const probePath = probe ? nfc(probe.location().toString()) : null;
    if (probePath && probePath !== toAdd[0].path) {
      report.copied = true;
      report.copiedTo = probePath;
      Music.delete(probe);
      return JSON.stringify(report);
    }
    for (let start = 1; start < toAdd.length; start += CHUNK) {
      const chunk = toAdd.slice(start, start + CHUNK);
      try {
        Music.add(chunk.map(function (item) { return Path(item.path); }));
      } catch (error) {
        // Délai dépassé sur un gros lot : Apple Music continue l'import, la
        // relecture qui suit retrouve ce qui est arrivé.
        log('  (lot ' + (start + 1) + ' : ' + error + ')');
      }
      log('  ' + Math.min(start + CHUNK, toAdd.length) + '/' + toAdd.length);
    }
    inv = inventory();
    for (const item of toAdd) {
      const pid = inv.byPath[item.path];
      if (pid) {
        pidOf[item.id] = pid;
        report.added++;
      } else {
        report.missing.push(item.path);
      }
    }
  } else {
    // Essai : les titres à ajouter comptent comme s'ils l'étaient déjà, pour
    // que le décompte des playlists soit juste.
    report.added = toAdd.length;
    for (const item of toAdd) pidOf[item.id] = 'nouveau:' + item.id;
  }

  // -------------------------------------------------- genre, regroupement

  log('Genres et regroupements…');
  for (const item of input.items) {
    const pid = pidOf[item.id];
    if (!pid || !inv.byPid[pid]) continue;
    const current = inv.byPid[pid];
    const grouping = item.grouping || '';
    if (current.genre !== item.genre || (current.grouping || '') !== grouping) {
      if (!input.dryRun) {
        const track = library.fileTracks[current.index];
        track.genre = item.genre;
        track.grouping = grouping;
      }
      report.retagged++;
    }
  }

  // ---------------------------------------------------------- playlists

  log('Dossiers et playlists…');
  // Dossiers gérés : ceux du plan, et ceux des lancements précédents (une
  // famille entière peut avoir quitté le plan).
  const families = {};
  for (const family of input.families || []) families[family] = true;
  for (const playlist of input.playlists) families[playlist.family] = true;
  const plannedFamilies = {};
  for (const playlist of input.playlists) plannedFamilies[playlist.family] = true;

  const folders = {};
  for (const folder of Music.folderPlaylists()) {
    const name = folder.name();
    if (families[name] && !folders[name]) folders[name] = folder;
  }
  for (const family of Object.keys(plannedFamilies)) {
    if (!family || folders[family]) continue;
    report.createdFolders.push(family);
    if (!input.dryRun) {
      folders[family] = makePlaylist(Music, 'folderPlaylist', family);
    }
  }

  // Playlists déjà rangées dans un dossier de famille : « Famille/Nom ».
  const existing = {};
  for (const playlist of Music.userPlaylists()) {
    if (playlist.class() === 'folderPlaylist') continue;
    let parent = null;
    try {
      parent = playlist.parent().name();
    } catch (error) {
      continue;
    }
    if (families[parent]) existing[parent + '/' + playlist.name()] = playlist;
  }

  const planned = {};
  for (const wanted of input.playlists) {
    const key = wanted.family + '/' + wanted.name;
    planned[key] = true;
    const ids = wanted.ids.filter(function (id) { return pidOf[id]; });
    let playlist = existing[key];
    if (!playlist) {
      report.createdPlaylists.push(key);
      if (input.dryRun) {
        report.trackAdds += ids.length;
        continue;
      }
      const made = makePlaylist(Music, 'userPlaylist', wanted.name);
      const madeId = made.persistentID();
      Music.move(made, { to: folders[wanted.family] });
      playlist = Music.userPlaylists.whose({ persistentID: madeId })[0];
      // Une playlist restée hors de son dossier ne serait pas retrouvée au
      // lancement suivant, et serait recréée à chaque fois : mieux vaut
      // s'arrêter tout de suite.
      let parent = null;
      try { parent = playlist.parent().name(); } catch (error) { parent = null; }
      if (parent !== wanted.family) {
        Music.delete(playlist);
        throw new Error('impossible de ranger la playlist « ' + wanted.name +
          ' » dans le dossier « ' + wanted.family + ' »');
      }
    }

    const wantedPids = ids.map(function (id) { return pidOf[id]; });
    const wantedSet = {};
    for (const pid of wantedPids) wantedSet[pid] = true;
    const current = every(playlist.tracks, 'persistentID');
    const currentSet = {};
    for (const pid of current) currentSet[pid] = true;

    // Retraits d'abord, de la fin vers le début : les indices restent justes.
    for (let j = current.length - 1; j >= 0; j--) {
      if (!wantedSet[current[j]]) {
        if (!input.dryRun) Music.delete(playlist.tracks[j]);
        report.trackRemovals++;
      }
    }
    for (const pid of wantedPids) {
      if (currentSet[pid]) continue;
      if (!input.dryRun) Music.duplicate(library.fileTracks[inv.byPid[pid].index], { to: playlist });
      report.trackAdds++;
    }
    log('  ' + key + ' : ' + wantedPids.length + ' titre(s)');
  }

  for (const key of Object.keys(existing)) {
    if (!planned[key]) report.stale.push(key);
  }

  report.known = pidOf;
  return JSON.stringify(report);
}
