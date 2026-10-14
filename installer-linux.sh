#!/bin/sh
# BlokBambu pour Linux. Installe ou met à jour BlokBambu : copie compteur.py dans le dossier des
# données, le lance, et le fait démarrer à chaque ouverture de session.
#
#   installer-linux.sh             installe ou met à jour
#   installer-linux.sh --retirer   arrête BlokBambu et l'enlève ; les données sont gardées
#   --discret                      n'ouvre pas la page à la fin
set -e

DEPOT="https://raw.githubusercontent.com/Blokstats/blokbambu/main"
PORT=47815
PAGE="http://127.0.0.1:$PORT/"
DONNEES="${XDG_DATA_HOME:-$HOME/.local/share}/blokbambu"
DEMARRAGE="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/blokbambu.desktop"
MENU="${XDG_DATA_HOME:-$HOME/.local/share}/applications/blokbambu.desktop"
FICHIERS="compteur.py page.html police.woff2 version.txt"

[ "$(uname)" = "Linux" ] || { echo "Cet installeur est celui de Linux. Pour Mac : installer.sh ; pour Windows : installer.ps1."; exit 1; }

RETIRER=0
DISCRET=0
for a in "$@"; do
  case "$a" in
    --retirer) RETIRER=1 ;;
    --discret) DISCRET=1 ;;
  esac
done

arreter() {
  pkill -f "$DONNEES/compteur.py" 2>/dev/null || true
}

if [ "$RETIRER" = 1 ]; then
  arreter
  rm -f "$DEMARRAGE" "$MENU"
  echo "BlokBambu est retiré. Vos données restent dans $DONNEES"
  exit 0
fi

PYTHON=$(command -v python3 || true)
if [ -z "$PYTHON" ] || ! "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 7))' 2>/dev/null; then
  echo "Il manque Python 3 sur cet ordinateur. Installez-le, puis relancez l'installation de BlokBambu :"
  echo
  echo "    sudo apt install python3     (Debian, Ubuntu, Mint)"
  echo "    sudo dnf install python3     (Fedora)"
  exit 1
fi

mkdir -p "$DONNEES" "$(dirname "$DEMARRAGE")" "$(dirname "$MENU")"

# Les fichiers de BlokBambu : ceux posés à côté de ce script (dépôt téléchargé), sinon ceux du dépôt
# en ligne. Ils ne remplacent les anciens qu'une fois tous arrivés.
ICI=$(cd "$(dirname "$0")" 2>/dev/null && pwd || true)
for f in $FICHIERS; do
  if [ -n "$ICI" ] && [ -f "$ICI/compteur.py" ] && [ -f "$ICI/installer-linux.sh" ]; then
    cp "$ICI/$f" "$DONNEES/$f.nouveau"
  elif command -v curl >/dev/null 2>&1; then
    curl -fsSL "$DEPOT/$f" -o "$DONNEES/$f.nouveau" \
      || { echo "Téléchargement impossible : vérifiez la connexion à Internet."; exit 1; }
  else
    wget -q "$DEPOT/$f" -O "$DONNEES/$f.nouveau" \
      || { echo "Téléchargement impossible : vérifiez la connexion à Internet."; exit 1; }
  fi
done
arreter
for f in $FICHIERS; do mv "$DONNEES/$f.nouveau" "$DONNEES/$f"; done

# Démarrage à chaque ouverture de session.
cat > "$DEMARRAGE" <<FIN
[Desktop Entry]
Type=Application
Name=BlokBambu
Comment=Compteur de fil pour Bambu Studio
Exec="$PYTHON" "$DONNEES/compteur.py" --journal
NoDisplay=true
X-GNOME-Autostart-enabled=true
FIN

# Une entrée « BlokBambu » dans le menu des applications, qui ouvre la page.
cat > "$MENU" <<FIN
[Desktop Entry]
Type=Application
Name=BlokBambu
Comment=Fil par couleur et bobines
Exec=xdg-open $PAGE
Icon=printer
Terminal=false
Categories=Utility;
FIN

# Lancement tout de suite, détaché de ce terminal.
if command -v setsid >/dev/null 2>&1; then
  setsid "$PYTHON" "$DONNEES/compteur.py" --journal >/dev/null 2>&1 < /dev/null &
else
  nohup "$PYTHON" "$DONNEES/compteur.py" --journal >/dev/null 2>&1 < /dev/null &
fi

# La page répond dès que le programme a démarré.
"$PYTHON" - "$PAGE" <<'FIN' || { echo "BlokBambu n'a pas démarré. Son journal : $DONNEES/compteur.log"; exit 1; }
import sys, time, urllib.request
for _ in range(50):
    try:
        urllib.request.urlopen(sys.argv[1], timeout=2).close()
        sys.exit(0)
    except OSError:
        time.sleep(0.3)
sys.exit(1)
FIN

echo "BlokBambu est installé."
echo "  Page : $PAGE (aussi dans le menu des applications, sous « BlokBambu »)"
if [ ! -f "${XDG_CONFIG_HOME:-$HOME/.config}/BambuStudio/BambuStudio.conf" ] \
   && [ ! -f "$HOME/.var/app/com.bambulab.BambuStudio/config/BambuStudio/BambuStudio.conf" ]; then
  echo
  echo "Bambu Studio n'a pas été trouvé sur cet ordinateur. Installez-le et connectez-le à votre imprimante :"
  echo "BlokBambu s'en servira tout seul, sans réinstallation."
fi
if [ "$DISCRET" != 1 ]; then
  xdg-open "$PAGE" >/dev/null 2>&1 || true
fi
