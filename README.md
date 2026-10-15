# BlokBambu

Un compteur de fil pour les imprimantes Bambu Lab, sur Mac, Windows et Linux.

- **Fil par couleur** : chaque impression lancée depuis Bambu Studio est notée, avec les grammes et les mètres de chaque fil. Une impression arrêtée en route ne compte que pour la part faite.
- **Bobines** : la page montre ce qu'il reste sur chaque bobine, avec sa marque, son prix, son emplacement sur l'imprimante et vos bobines en réserve. Plusieurs bobines de la même couleur se suivent côte à côte.
- **Imprimantes** : chacune porte le nom que vous lui donnez. La page montre ce qu'elle imprime, les bobines qu'elle porte, et ce qu'elle a consommé : fil, impressions, temps, coût.
- **Coût et temps** : avec le prix de vos bobines, chaque impression et chaque mois sont chiffrés ; le temps d'impression est totalisé par mois et par imprimante.
- **Alertes** : une fenêtre vous prévient quand une bobine est chargée, changée ou retirée, quand une impression demande plus de fil qu'il n'en reste, quand la dernière bobine d'une couleur arrive au bout, et quand une impression est terminée ou s'est arrêtée.
- **Impressions lancées ailleurs** : celles du téléphone ou de l'écran de l'imprimante apparaissent aussi. Vous indiquez leur fil, ou BlokBambu le lit sur l'imprimante si vous l'y autorisez.

Tout tourne sur votre ordinateur. Vos données n'en sortent pas, et BlokBambu n'envoie aucune commande à l'imprimante.

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
2. Les bobines chargées sur l'imprimante y sont déjà. Pour chacune, cliquez **Suivre cette bobine**, choisissez sa marque (ou ajoutez la vôtre) et indiquez le poids de fil qu'il reste dessus. Le prix au kilo et le nombre de bobines en réserve sont facultatifs.
3. Imprimez comme d'habitude depuis Bambu Studio : chaque impression apparaît dans la page, sans la recharger, et se déduit de la bobine qui fournit le fil.
4. Quand vous posez une bobine neuve, une fenêtre vous le rappelle : cliquez **Nouvelle bobine** sur la page. Elle sort de la réserve.

### Les bobines

- Elles sont rangées par emplacement : **A1, A2, A3, A4**, puis celles qui ne sont pas sur l'imprimante.
- L'emplacement vient de ce que l'imprimante annonce. Si elle ne le sait pas, ou se trompe, cliquez sur l'emplacement de la bobine (ou sur **Modifier**) et choisissez-le vous-même.
- **+ Ajouter une bobine** accepte une couleur déjà suivie : deux bobines blanches de marques ou de prix différents se suivent séparément. Une impression se déduit de celle qui est sur l'imprimante ; vous pouvez changer de bobine dans la fiche de l'impression.
- **Bientôt vide**, en rose : il reste moins de 100 g. Ce seuil se change dans les réglages.
- **À racheter**, en haut de page : une bobine bientôt vide dont la réserve est à zéro.
- **Bobine finie en pleine impression** : posez la neuve et cliquez **Nouvelle bobine** (ou posez une autre bobine suivie sur cet emplacement). Elle prend le relais : la suite de l'impression se déduit d'elle, à partir de l'avancement où l'ancienne s'est vidée. Quand l'AMS passe tout seul sur une autre bobine du même fil, c'est noté aussi. La fiche de l'impression permet de le corriger, ou de le dire à la main (« Bobine changée en cours d'impression »).
- Pour corriger un reste qui a dérivé, cliquez **Nouvelle bobine** et saisissez le poids réel.
- Les bobines Bambu Lab portent une puce : leur reste est lu dessus, sans saisie.

### Les imprimantes

Celles que Bambu Studio connaît sur cet ordinateur sont suivies d'office. Dans les réglages (la roue, en haut à droite) :

- **Nommer** une imprimante : écrivez son nom dans la liste.
- **Ajouter** une imprimante que Bambu Studio ne connaît pas ici : donnez son adresse IP et son code d'accès. Les deux se lisent sur l'écran de l'imprimante, dans Réglages › WLAN (ou Réseau). L'imprimante doit être allumée à ce moment-là.
- **Retirer** une imprimante : elle n'est plus suivie, ses impressions restent dans la page.

Avec plusieurs imprimantes, chaque impression porte le nom de la sienne et la liste se filtre par imprimante.

### Les impressions

- **Modifier**, sur une impression : la part réellement imprimée, la bobine qui a fourni chaque fil (et celle qui a pris le relais en cours de route), l'imprimante, ou la supprimer.
- **+ Ajouter une impression** : pour noter à la main une impression que BlokBambu n'a pas vue.
- Une impression lancée **depuis le téléphone ou l'écran de l'imprimante** apparaît d'elle-même, avec son nom, son heure, son avancement et l'emplacement utilisé. Son poids n'est pas connu : cliquez **Compléter** pour l'indiquer.
- Pour ne pas avoir à le saisir, activez dans les réglages « Poids des impressions lancées ailleurs : lu sur l'imprimante ». Une fois l'impression finie, BlokBambu lit alors son fichier sur la carte mémoire de l'imprimante. Il ne fait que lire.

### Depuis le téléphone

Dans les réglages, activez « Voir BlokBambu depuis le téléphone ». La page donne alors une adresse et un code à six chiffres : ouvrez l'adresse sur un téléphone relié au même Wi-Fi et saisissez le code. L'ordinateur doit rester allumé, et peut demander d'autoriser les connexions entrantes.

### Mettre à jour, partager

- Quand une nouvelle version paraît, la page l'annonce en haut, avec la ligne à relancer. Rien ne s'installe tout seul.
- Dans les réglages, **Copier le lien** donne l'adresse de cette page, à envoyer à vos amis.

## À savoir

- Les poids sont ceux que Bambu Studio estime au lancement de l'impression, purge comprise. Pour une impression arrêtée en route, BlokBambu retient l'avancement annoncé par l'imprimante : c'est une estimation, que vous pouvez corriger.
- Le fil est rattaché à la bobine qui l'a vraiment fourni quand l'imprimante est connectée ; sinon, à la couleur réglée dans le projet. Avec plusieurs fils, c'est l'emplacement utilisé de la couleur la plus proche qui est retenu.
- Le temps d'impression est celui que Bambu Studio prévoit. Pour une impression lancée ailleurs, c'est le temps observé sur l'imprimante, pauses déduites.
- Une impression lancée ailleurs n'est vue que si l'ordinateur est allumé pendant qu'elle tourne.
- L'alerte de bobine et l'alerte de fin demandent que l'ordinateur soit allumé et sur le même réseau que l'imprimante.
- La version Mac sert tous les jours, avec une Bambu Lab A1. Les versions Windows et Linux passent les mêmes essais automatiques, mais n'ont pas encore tourné avec une vraie imprimante : dites-moi si quelque chose coince.

## Si quelque chose coince

- **La page ne s'ouvre pas.** Relancez la ligne d'installation : elle relance BlokBambu sans toucher à vos données.
- **« Imprimante injoignable ».** Vérifiez que l'imprimante est allumée et sur le même réseau, puis ouvrez Bambu Studio une fois : c'est par lui que BlokBambu apprend l'adresse de l'imprimante. Sur Mac, vérifiez aussi que « Python » est autorisé dans Réglages Système › Confidentialité et sécurité › Réseau local.
- **Une impression n'apparaît pas.** Lancée depuis Bambu Studio sur cet ordinateur, avec le bouton « Imprimer », elle apparaît quelques secondes après l'envoi. Lancée ailleurs, elle apparaît deux minutes après son départ, si l'imprimante est connectée. Sinon, ajoutez-la à la main.
- **Le poids d'une impression lancée ailleurs n'est pas lu.** La lecture se fait à la fin de l'impression, et demande une carte mémoire dans l'imprimante. Si le fichier n'y est plus, indiquez le poids à la main.
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
- **L'imprimante.** BlokBambu écoute ce qu'elle annonce sur le réseau local, comme le fait Bambu Studio : les bobines chargées, l'impression en cours, son avancement, sa fin. Pour s'y connecter, il lit le code d'accès que Bambu Studio garde déjà sur l'ordinateur, et vérifie que c'est bien une imprimante Bambu Lab, la vôtre, avant de le présenter. Il écoute seulement : il ne peut ni lancer ni arrêter une impression.
- **La carte mémoire de l'imprimante**, seulement si vous l'activez dans les réglages : à la fin d'une impression lancée ailleurs, BlokBambu y lit le fichier de cette impression pour connaître son poids. Il n'écrit rien, n'efface rien.
- **La page** n'est visible que depuis votre ordinateur (`127.0.0.1`), sauf si vous l'ouvrez au téléphone dans les réglages : elle demande alors un code aux autres appareils du réseau.

Ce qui sort de l'ordinateur, et ce qui n'en sort pas :

- Vos impressions, vos bobines et vos réglages restent dans le dossier de données de BlokBambu.
- Le code d'accès lu dans Bambu Studio n'est recopié nulle part. Celui d'une imprimante ajoutée à la main est gardé dans ce dossier, dans un fichier lisible par votre seule session ; la page ne le montre jamais.
- Une fois par jour, BlokBambu lit sur GitHub le numéro de la dernière version (un fichier public de ce dépôt). Il n'envoie rien.

Les titres de la page sont en Archivo, une police libre (SIL Open Font License, voir `LICENCE-police.txt`).

## Dans ce dépôt

| Fichier | Rôle |
|---|---|
| `compteur.py` | le programme : repère les impressions, écoute l'imprimante, sert la page |
| `page.html`, `police.woff2` | la page et la police de ses titres |
| `version.txt` | le numéro de la version, et ce qu'elle apporte en une ligne |
| `alerte.swift` | la fenêtre d'alerte sur Mac, aux couleurs de la page (sur Windows et Linux, c'est une fenêtre du système) |
| `installer.sh`, `installer.ps1`, `installer-linux.sh` | les installeurs pour Mac, Windows et Linux |
| `tests/` | les essais, rejoués sur macOS, Windows et Linux à chaque modification, avec de fausses imprimantes |
