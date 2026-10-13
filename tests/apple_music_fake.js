// Fausse application Music, pour faire tourner apple_music.js sous Node.
//
//   node apple_music_fake.js SCRIPT ÉTAT ENTRÉE
//
// ÉTAT est un JSON qui décrit la bibliothèque avant, et qui est réécrit après :
//   { copies: bool, tracks: [{pid, path, genre, grouping}],
//     playlists: [{pid, name, folder: bool, parent: pid|null, tracks: [pid…]}] }
// Le script s'exécute tel quel ; seuls les objets que macOS fournirait sont imités.

const fs = require('fs');
const [scriptPath, statePath, inputPath] = process.argv.slice(2);
const state = JSON.parse(fs.readFileSync(statePath, 'utf8'));
let counter = 1000;
const newPid = () => 'P' + (counter++);

function Path(path) {
  return { toString: () => path, _path: path };
}

// Collection à la façon JXA : appeler une propriété en renvoie le tableau, un
// indice renvoie l'élément.
function collection(getItems, wrap) {
  // Appelée, la collection renvoie ses éléments ; une propriété appelée
  // renvoie la liste de ses valeurs ; un indice renvoie un élément.
  return new Proxy(function () {}, {
    apply() {
      return getItems().map(wrap);
    },
    get(_, prop) {
      if (typeof prop === 'string' && /^\d+$/.test(prop)) return wrap(getItems()[Number(prop)]);
      if (prop === 'length') return getItems().length;
      if (prop === 'whose') {
        return (filter) => getItems()
          .filter((item) => Object.keys(filter).every((k) => item[k] === filter[k]))
          .map(wrap);
      }
      // Comme Music : lire une propriété de « tous les éléments » d'une
      // collection vide échoue au lieu de renvoyer une liste vide.
      return () => {
        if (!getItems().length) throw new Error("Il est impossible d’obtenir l’objet. (-1728)");
        return getItems().map((item) => {
        const value = item[prop];
        return prop === 'location' ? (value ? Path(value) : null) : value;
        });
      };
    },
  });
}

// Comme en JXA, une propriété se lit en l'appelant et s'écrit en l'affectant.
function trackObject(track) {
  return {
    _track: track,
    persistentID: () => track.pid,
    get location() { return () => Path(track.path); },
    set location(path) { track.path = path._path; },
    get genre() { return () => track.genre; },
    set genre(value) { track.genre = value; },
    get grouping() { return () => track.grouping; },
    set grouping(value) { track.grouping = value; },
    // « favorited » depuis macOS 14 ; « loved » avant (état « ancienMacOS »).
    get favorited() {
      return () => {
        if (state.ancienMacOS) throw new Error('Message incompréhensible. (-1708)');
        return Boolean(track.favorite);
      };
    },
    set favorited(value) {
      if (state.ancienMacOS) throw new Error('Message incompréhensible. (-1708)');
      track.favorite = value;
    },
    get loved() { return () => Boolean(track.favorite); },
    set loved(value) { track.favorite = value; },
  };
}

function playlistObject(playlist) {
  return {
    _playlist: playlist,
    name: () => playlist.name,
    class: () => (playlist.folder ? 'folderPlaylist' : 'userPlaylist'),
    persistentID: () => playlist.pid,
    parent: () => {
      const parent = state.playlists.find((p) => p.pid === playlist.parent);
      if (!parent) throw new Error("Can't get object.");
      return playlistObject(parent);
    },
    tracks: collection(
      () => playlist.tracks.map((pid) => ({ persistentID: pid })),
      (entry) => ({ _entry: { playlist, pid: entry.persistentID } }),
    ),
  };
}

function addFile(path) {
  const copied = state.copies ? '/Users/x/Music/Music/Media.localized/' + path.split('/').pop() : path;
  const track = { pid: newPid(), path: copied, genre: '', grouping: '' };
  state.tracks.push(track);
  return trackObject(track);
}


const Music = {
  libraryPlaylists: [{
    fileTracks: collection(
      () => state.tracks.map((t) => ({ ...t, persistentID: t.pid, location: t.path, _t: t })),
      (t) => trackObject(t._t)),
  }],
  add(what) {
    if (Array.isArray(what)) return what.map((p) => addFile(p._path));
    return addFile(what._path);
  },
  delete(object) {
    if (object._entry) {
      const { playlist, pid } = object._entry;
      playlist.tracks.splice(playlist.tracks.indexOf(pid), 1);
    } else if (object._playlist) {
      state.playlists = state.playlists.filter((p) => p !== object._playlist);
    } else if (object._track) {
      state.tracks = state.tracks.filter((t) => t !== object._track);
    }
  },
  // Comme Music : pas de « folderPlaylists » ni de « userPlaylists » au niveau
  // de l'application (-1708) ; seulement « playlists », dont la bibliothèque.
  playlists: collection(
    () => [{ pid: 'LIB', name: 'Bibliothèque', kind: 'libraryPlaylist', tracks: [] }]
      .concat(state.playlists)
      .map((p) => ({ ...p, persistentID: p.pid, _p: p,
        class: p.kind || (p.folder ? 'folderPlaylist' : 'userPlaylist') })),
    (p) => playlistObject(p._p)),
  make({ new: kind, withProperties }) {
    const playlist = { pid: newPid(), name: withProperties.name, folder: kind === 'folderPlaylist',
      parent: null, tracks: [] };
    state.playlists.push(playlist);
    return playlistObject(playlist);
  },
  move(playlist, { to }) {
    playlist._playlist.parent = to._playlist.pid;
  },
  duplicate(track, { to }) {
    to._playlist.tracks.push(track._track.pid);
  },
};

global.Application = (name) => {
  if (name !== 'Music') throw new Error('application inattendue : ' + name);
  return Music;
};
global.Path = Path;
global.ObjC = { import: () => {}, unwrap: (value) => value };
global.$ = {
  NSUTF8StringEncoding: 4,
  NSString: { stringWithContentsOfFileEncodingError: (path) => fs.readFileSync(path, 'utf8') },
};
console.log = (message) => process.stderr.write(message + '\n');

const source = fs.readFileSync(scriptPath, 'utf8');
const run = new Function(source + '\nreturn run;')();
const output = run([inputPath]);
fs.writeFileSync(statePath, JSON.stringify(state));
process.stdout.write(output);
