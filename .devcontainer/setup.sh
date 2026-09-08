#!/usr/bin/env bash
# Préparation du Codespace : dépendances et configuration de départ.
set -euo pipefail

pip install --upgrade pip
pip install -e ".[web,dev]"

if [ ! -f config/config.toml ]; then
  cp config/config.example.toml config/config.toml
  # Dans un Codespace, l'interface doit écouter sur toutes les interfaces pour
  # que le port transféré la voie. Le jeton d'accès devient alors obligatoire :
  # il est engendré au démarrage si YTMGC_ACCESS_TOKEN n'est pas défini.
  sed -i 's/^host = "127.0.0.1"/host = "0.0.0.0"/' config/config.toml
  echo "config/config.toml créé (écoute sur 0.0.0.0, jeton d'accès exigé)."
fi

echo
echo "Prêt. Lance l'interface avec :  ./start"
