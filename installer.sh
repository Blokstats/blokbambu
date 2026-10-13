#!/bin/zsh
# Installe ou met à jour BlokBambu sur ce Mac : copie compteur.py hors du Bureau, lance le
# programme de fond et pose le raccourci de la page.
#
#   installer.sh              installe ; le raccourci de la page va sur le Bureau
#   installer.sh <dossier>    le raccourci va dans ce dossier (retenu pour les fois suivantes),
#                             et les couleurs des projets Bambu du dossier sont proposées sur la page
#   installer.sh --retirer    enlève le programme de fond et le raccourci ; les données sont gardées
#   --discret                 n'ouvre pas la page à la fin
set -e

NOM=fr.blokcorp.blokbambu
PORT=47815
DEPOT="https://raw.githubusercontent.com/Blokstats/blokbambu/main"
DONNEES="$HOME/Library/Application Support/BlokBambu"
AGENT="$HOME/Library/LaunchAgents/$NOM.plist"
PAGE="BlokBambu.html"
BAMBU="$HOME/Library/Application Support/BambuStudio/BambuStudio.conf"

[[ "$(uname)" == "Darwin" ]] || { echo "BlokBambu ne fonctionne que sur macOS."; exit 1; }

RETIRER=0; DISCRET=0; DOSSIER=""
for a in "$@"; do
  case "$a" in
    --retirer) RETIRER=1 ;;
    --discret) DISCRET=1 ;;
    *) DOSSIER="$a" ;;
  esac
done

# Le dossier du raccourci choisi à une installation précédente.
ANCIEN=""
[[ -f "$DONNEES/dossier.txt" ]] && ANCIEN="$(<"$DONNEES/dossier.txt")"

launchctl bootout "gui/$UID/$NOM" 2>/dev/null || true
while launchctl print "gui/$UID/$NOM" &>/dev/null; do sleep 0.2; done

if (( RETIRER )); then
  rm -f "$AGENT"
  LIEN="${ANCIEN:-$HOME/Desktop}/$PAGE"
  [[ -L "$LIEN" ]] && rm "$LIEN"
  echo "BlokBambu est retiré. Vos données restent dans $DONNEES"
  exit 0
fi

# Python : celui de macOS (outils de ligne de commande d'Apple), sinon un autre déjà installé.
bon_python() { "$1" -c 'import sys; sys.exit(sys.version_info < (3, 7))' 2>/dev/null; }
PYTHON=""
if xcode-select -p &>/dev/null && bon_python /usr/bin/python3; then
  PYTHON=/usr/bin/python3
elif command -v python3 &>/dev/null && [[ "$(command -v python3)" != /usr/bin/python3 ]] && bon_python "$(command -v python3)"; then
  PYTHON="$(command -v python3)"
else
  echo "Il manque Python sur ce Mac. Lancez cette commande, acceptez l'installation proposée par macOS,"
  echo "puis relancez l'installation de BlokBambu :"
  echo
  echo "    xcode-select --install"
  exit 1
fi

mkdir -p "$DONNEES" "$HOME/Library/LaunchAgents"

# compteur.py : celui posé à côté de ce script (dépôt téléchargé), sinon celui du dépôt en ligne.
ICI=${0:A:h}
if [[ -f "$ICI/compteur.py" ]]; then
  cp "$ICI/compteur.py" "$DONNEES/compteur.py"
else
  curl -fsSL "$DEPOT/compteur.py" -o "$DONNEES/compteur.py.telecharge" \
    || { echo "Téléchargement impossible : vérifiez la connexion à Internet."; exit 1; }
  mv "$DONNEES/compteur.py.telecharge" "$DONNEES/compteur.py"
fi

# Le dossier du raccourci : celui demandé, sinon celui de la dernière fois, sinon le Bureau.
[[ -n "$DOSSIER" ]] && DOSSIER="${DOSSIER:A}"
[[ -z "$DOSSIER" ]] && DOSSIER="${ANCIEN:-$HOME/Desktop}"
[[ -d "$DOSSIER" ]] || { echo "Dossier introuvable : $DOSSIER"; exit 1; }
if [[ -n "$ANCIEN" && "$ANCIEN" != "$DOSSIER" && -L "$ANCIEN/$PAGE" ]]; then rm "$ANCIEN/$PAGE"; fi
print -r -- "$DOSSIER" > "$DONNEES/dossier.txt"

# Un dossier de projets Bambu : ses couleurs sont proposées à l'ajout d'une bobine. Le Bureau entier
# n'est pas parcouru.
if [[ "$DOSSIER" != "$HOME/Desktop" ]]; then
  "$PYTHON" -I "$DONNEES/compteur.py" --couleurs "$DOSSIER" >/dev/null
else
  "$PYTHON" -I "$DONNEES/compteur.py" --page
fi

cat > "$AGENT" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$NOM</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>-I</string>
    <string>$DONNEES/compteur.py</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$DONNEES/compteur.log</string>
  <key>StandardErrorPath</key><string>$DONNEES/compteur.log</string>
</dict>
</plist>
EOF

launchctl bootstrap "gui/$UID" "$AGENT"
# La page vit hors du Bureau (un programme de fond n'a pas le droit d'y écrire sans autorisation) :
# le dossier choisi n'en contient qu'un raccourci.
ln -sfn "$DONNEES/$PAGE" "$DOSSIER/$PAGE"

# La page répond dès que le programme de fond a démarré.
for i in {1..50}; do
  curl -fs -o /dev/null "http://127.0.0.1:$PORT/" && break
  sleep 0.3
done

echo "BlokBambu est installé."
echo "  Page      : http://127.0.0.1:$PORT/"
echo "  Raccourci : $DOSSIER/$PAGE"
if [[ ! -f "$BAMBU" ]]; then
  echo
  echo "Bambu Studio n'a pas été trouvé sur ce Mac. Installez-le et connectez-le à votre imprimante :"
  echo "BlokBambu s'en servira tout seul, sans réinstallation."
fi
echo
echo "Si macOS demande d'autoriser « Python » à accéder au réseau local, acceptez : c'est ce qui"
echo "permet d'être prévenu quand une bobine est chargée ou retirée."
(( DISCRET )) || open "http://127.0.0.1:$PORT/"
