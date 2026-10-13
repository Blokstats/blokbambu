# BlokBambu

Un compteur de fil pour les imprimantes Bambu Lab, sur Mac, Windows et Linux.

- **Fil par couleur** : chaque impression lancée depuis Bambu Studio est notée, avec les grammes et les mètres de chaque fil.
- **Bobines** : la page montre ce qu'il reste sur chaque bobine, avec sa marque. Un bouton « Nouvelle bobine » la remet à neuf.
- **Alerte** : une fenêtre vous prévient quand une bobine est chargée, changée ou retirée de l'imprimante, pour penser à mettre la page à jour.

Tout tourne sur votre ordinateur. Rien n'est envoyé sur Internet, et BlokBambu n'envoie aucune commande à l'imprimante.

![La page de BlokBambu](apercu.png)

## Ce qu'il faut

- Un ordinateur sous macOS, Windows 10 ou 11, ou Linux.
- Bambu Studio, déjà connecté à votre imprimante.
- L'ordinateur et l'imprimante sur le même réseau, pour l'alerte de bobine.

## Installer

Une seule ligne à coller. La page s'ouvre toute seule à la fin, et BlokBambu se relance ensuite à chaque ouverture de session : il n'y a rien à lancer à la main.

Pour mettre à jour plus tard, relancez la même ligne : vos données sont gardées.

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
2. Les bobines chargées sur l'imprimante y sont déjà. Pour chacune, cliquez **Suivre cette bobine**, choisissez sa marque (ou ajoutez la vôtre) et indiquez le poids de fil qu'il reste dessus.
3. Imprimez comme d'habitude depuis Bambu Studio : chaque impression apparaît dans la page, sans la recharger, et se déduit de la bobine de la même couleur.
4. Quand vous posez une bobine neuve, une fenêtre vous le rappelle : cliquez **Nouvelle bobine** sur la page.

Sur la page :

- **A1 à A4** sur une bobine : son emplacement sur l'AMS de l'imprimante.
- **Bientôt vide**, en rose : il reste moins de 100 g.
- **Imprimante connectée**, en haut à droite : l'alerte de bobine est active.
- Pour corriger un reste qui a dérivé, cliquez **Nouvelle bobine** et saisissez le poids réel.

## À savoir

- Les poids sont ceux que Bambu Studio estime au lancement de l'impression, purge comprise. Une impression annulée en route compte en entier.
- La couleur retenue est celle réglée dans le projet. Un projet réglé en noir et imprimé avec une bobine orange compte en noir.
- Seules les impressions lancées depuis Bambu Studio sur cet ordinateur sont comptées. Celles lancées depuis le téléphone ou l'écran de l'imprimante ne le sont pas.
- L'alerte de bobine demande que l'ordinateur soit allumé et sur le même réseau que l'imprimante.
- BlokBambu suit une imprimante : celle choisie en dernier dans Bambu Studio.
- La version Mac sert tous les jours. Les versions Windows et Linux passent les mêmes essais automatiques, mais n'ont pas encore tourné avec une vraie imprimante : dites-moi si quelque chose coince.

## Si quelque chose coince

- **La page ne s'ouvre pas.** Relancez la ligne d'installation : elle relance BlokBambu sans toucher à vos données.
- **« Imprimante injoignable ».** Vérifiez que l'imprimante est allumée et sur le même réseau, puis ouvrez Bambu Studio une fois : c'est par lui que BlokBambu apprend l'adresse de l'imprimante. Sur Mac, vérifiez aussi que « Python » est autorisé dans Réglages Système › Confidentialité et sécurité › Réseau local.
- **Une impression n'apparaît pas.** Elle doit être lancée depuis Bambu Studio, sur cet ordinateur, avec le bouton « Imprimer ». Elle apparaît quelques secondes après l'envoi.
- **Autre chose.** BlokBambu tient un journal, `compteur.log`, dans son dossier de données (indiqué dans « Retirer » ci-dessous). Envoyez-le-moi.

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

BlokBambu est un petit programme en Python (`compteur.py`, sans dépendance) qui reste lancé en fond et sert la page (`page.html`).

- **Les impressions.** Quand vous cliquez sur « Imprimer », Bambu Studio écrit sur l'ordinateur le fichier qu'il envoie à l'imprimante. BlokBambu le repère et y lit le poids de chaque fil.
- **Les bobines.** BlokBambu écoute ce que l'imprimante annonce sur le réseau local, comme le fait Bambu Studio. Pour s'y connecter, il lit le code d'accès que Bambu Studio garde déjà sur l'ordinateur, et vérifie que c'est bien votre imprimante avant de le présenter. Il écoute seulement : il ne peut ni lancer ni arrêter une impression.
- **La page** n'est visible que depuis votre ordinateur (`127.0.0.1`).

Le code d'accès n'est recopié nulle part, et aucune donnée ne quitte l'ordinateur.

Les titres de la page sont en Archivo, une police libre (SIL Open Font License, voir `LICENCE-police.txt`).

## Dans ce dépôt

| Fichier | Rôle |
|---|---|
| `compteur.py` | le programme : repère les impressions, écoute l'imprimante, sert la page |
| `page.html`, `police.woff2` | la page et la police de ses titres |
| `installer.sh`, `installer.ps1`, `installer-linux.sh` | les installeurs pour Mac, Windows et Linux |
| `tests/` | les essais, rejoués sur macOS, Windows et Linux à chaque modification, avec une fausse imprimante |
