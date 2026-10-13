# BlokBambu

Un compteur de fil pour les imprimantes Bambu Lab, sur Mac, Windows et Linux.

- **Fil par couleur** : chaque impression lancée depuis Bambu Studio est notée, avec les grammes et les mètres de chaque fil.
- **Bobines** : la page montre ce qu'il reste sur chaque bobine. Un bouton « Nouvelle bobine » la remet à neuf.
- **Alerte** : une fenêtre vous prévient quand une bobine est chargée, changée ou retirée de l'imprimante, pour penser à mettre la page à jour.

Tout tourne sur votre ordinateur. Rien n'est envoyé sur Internet, et BlokBambu n'envoie aucune commande à l'imprimante.

![La page de BlokBambu](apercu.png)

## Ce qu'il faut

- Bambu Studio, déjà connecté à votre imprimante.
- L'ordinateur et l'imprimante sur le même réseau, pour l'alerte de bobine.

## Installer

La page s'ouvre toute seule à la fin de l'installation. Pour mettre à jour plus tard, relancez la même ligne : vos données sont gardées.

### Mac

Ouvrez l'application **Terminal**, collez cette ligne et validez :

```bash
curl -fsSL https://raw.githubusercontent.com/Blokstats/blokbambu/main/installer.sh | zsh
```

Un raccourci `BlokBambu.html` est posé sur le Bureau. macOS peut poser deux questions, à accepter toutes les deux :

- **Installer les outils de ligne de commande** : c'est Python, dont BlokBambu a besoin. Une fois l'installation d'Apple terminée, relancez la ligne ci-dessus.
- **Autoriser « Python » à accéder au réseau local** : c'est ce qui permet d'être prévenu quand une bobine change. Si vous avez refusé, l'autorisation se donne dans Réglages Système › Confidentialité et sécurité › Réseau local.

### Windows

Ouvrez **PowerShell** (menu Démarrer, tapez « PowerShell »), collez cette ligne et validez :

```powershell
irm https://raw.githubusercontent.com/Blokstats/blokbambu/main/installer.ps1 | iex
```

Un raccourci « BlokBambu » est posé sur le Bureau. Si Python n'est pas encore sur l'ordinateur, l'installeur le met en place tout seul.

### Linux

Dans un terminal :

```bash
curl -fsSL https://raw.githubusercontent.com/Blokstats/blokbambu/main/installer-linux.sh | sh
```

BlokBambu apparaît dans le menu des applications.

## S'en servir

1. Ouvrez la page : le raccourci, ou <http://127.0.0.1:47815/>.
2. Les bobines chargées sur l'imprimante y sont déjà. Pour chacune, cliquez **Suivre cette bobine** et indiquez le poids de fil qu'il reste dessus.
3. Imprimez comme d'habitude depuis Bambu Studio : chaque impression apparaît dans la page et se déduit de la bobine de la même couleur.
4. Quand vous posez une bobine neuve, une fenêtre vous le rappelle : cliquez **Nouvelle bobine** sur la page.

Une bobine passe en rouge quand il lui reste moins de 100 g.

## À savoir

- Les poids sont ceux que Bambu Studio estime au lancement de l'impression, purge comprise. Une impression annulée en route compte en entier.
- La couleur retenue est celle réglée dans le projet. Un projet réglé en noir et imprimé avec une bobine orange compte en noir.
- Seules les impressions lancées depuis Bambu Studio sur cet ordinateur sont comptées. Celles lancées depuis le téléphone ou l'écran de l'imprimante ne le sont pas.
- L'alerte de bobine demande que l'ordinateur soit allumé et sur le même réseau que l'imprimante.
- BlokBambu suit une imprimante : celle choisie en dernier dans Bambu Studio.
- La version Mac sert tous les jours. Les versions Windows et Linux passent les mêmes essais automatiques, mais n'ont pas encore tourné avec une vraie imprimante : dites-moi si quelque chose coince.

## Retirer

Mac :

```bash
curl -fsSL https://raw.githubusercontent.com/Blokstats/blokbambu/main/installer.sh | zsh -s -- --retirer
```

Windows :

```powershell
$env:BLOKBAMBU_ACTION = "retirer"; irm https://raw.githubusercontent.com/Blokstats/blokbambu/main/installer.ps1 | iex
```

Linux :

```bash
curl -fsSL https://raw.githubusercontent.com/Blokstats/blokbambu/main/installer-linux.sh | sh -s -- --retirer
```

Vos données sont gardées. Pour tout effacer, supprimez le dossier `BlokBambu` : `~/Library/Application Support/BlokBambu` sur Mac, `%APPDATA%\BlokBambu` sur Windows, `~/.local/share/blokbambu` sur Linux.

## Comment ça marche

BlokBambu est un petit programme en Python (`compteur.py`, sans dépendance) qui reste lancé en fond.

- **Les impressions.** Quand vous cliquez sur « Imprimer », Bambu Studio écrit sur l'ordinateur le fichier qu'il envoie à l'imprimante. BlokBambu le repère et y lit le poids de chaque fil.
- **Les bobines.** BlokBambu écoute ce que l'imprimante annonce sur le réseau local, comme le fait Bambu Studio. Pour s'y connecter, il lit le code d'accès que Bambu Studio garde déjà sur l'ordinateur, et vérifie que c'est bien votre imprimante avant de le présenter. Il écoute seulement : il ne peut ni lancer ni arrêter une impression.
- **La page** n'est visible que depuis votre ordinateur (`127.0.0.1`).

Le code d'accès n'est recopié nulle part, et aucune donnée ne quitte l'ordinateur.

Les essais (`tests/essais.py`) tournent sur macOS, Windows et Linux à chaque modification, avec une fausse imprimante.
