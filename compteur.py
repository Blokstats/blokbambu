#!/usr/bin/python3
"""BlokBambu — compte le fil utilisé par couleur à chaque impression lancée depuis Bambu Studio.

Quand on clique sur « Imprimer », Bambu Studio écrit le fichier envoyé à l'imprimante dans
son dossier de travail temporaire : <tmp>/bamboo_model/<jour>/<session>/Metadata/.<pid>.<n>.3mf
Ce fichier contient Metadata/slice_info.config, avec les grammes et les mètres par fil.
Le compteur surveille ce dossier, note chaque envoi dans un journal et tient la page à jour.
La page est servie sur cet ordinateur seulement (127.0.0.1) : c'est là qu'on suit et qu'on
remet à neuf les bobines.
Il écoute aussi les imprimantes sur le réseau local, sans rien leur envoyer : il en tire les
bobines chargées ou retirées (une fenêtre prévient), l'avancement et la fin de chaque impression,
et la bobine qui a vraiment fourni le fil.
Fonctionne sur macOS, Windows et Linux, avec la seule bibliothèque standard de Python.
"""
import argparse
import ftplib
import glob
import hashlib
import io
import ipaddress
import json
import os
import re
import secrets
import socket
import socketserver
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

MAC, WINDOWS = sys.platform == "darwin", sys.platform.startswith("win")
# Sous Windows, une commande lancée par un programme sans fenêtre en ferait surgir une.
SANS_FENETRE = {"creationflags": 0x08000000} if WINDOWS else {}


def dossier_donnees():
    if MAC:
        return os.path.expanduser("~/Library/Application Support/BlokBambu")
    if WINDOWS:
        return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "BlokBambu")
    return os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "blokbambu")


def dossiers_bambu():
    """Les dossiers où Bambu Studio garde ses réglages, selon le système."""
    if MAC:
        return [os.path.expanduser("~/Library/Application Support/BambuStudio")]
    if WINDOWS:
        return [os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "BambuStudio")]
    return [os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "BambuStudio"),
            os.path.expanduser("~/.var/app/com.bambulab.BambuStudio/config/BambuStudio")]  # Flatpak


def conf_bambu():
    chemins = [os.path.join(d, "BambuStudio.conf") for d in dossiers_bambu()]
    return next((c for c in chemins if os.path.exists(c)), chemins[0])


def ressources_bambu():
    """Les dossiers de ressources de Bambu Studio installé, là où leur emplacement est connu."""
    if MAC:
        return ["/Applications/BambuStudio.app/Contents/Resources",
                os.path.expanduser("~/Applications/BambuStudio.app/Contents/Resources")]
    if WINDOWS:
        return [os.path.join(os.environ[v], "Bambu Studio", "resources")
                for v in ("ProgramFiles", "ProgramFiles(x86)") if os.environ.get(v)]
    return []


def certificats_installes():
    """Le certificat livré avec Bambu Studio, là où son emplacement est connu : il suit ses mises à jour."""
    return [c for c in (os.path.join(b, "cert", "printer.cer") for b in ressources_bambu()) if os.path.exists(c)]


TEXTES_ERREURS = {}  # modèle d'imprimante → {code: texte}


def textes_erreurs(serie, conf=None):
    """{code: texte} des erreurs de ce modèle d'imprimante, tels que Bambu Studio les garde sur
    l'ordinateur — en français s'il les a. Vide si rien n'est trouvé : la page montre alors le code."""
    modele = (serie or "")[:3]
    if modele in TEXTES_ERREURS:
        return TEXTES_ERREURS[modele]
    dossiers = ([os.path.join(os.path.dirname(conf), "hms")] if conf else []) + \
        [os.path.join(d, "hms") for d in dossiers_bambu() + ressources_bambu()]
    textes = {}
    for langue in ("fr", "en", "*"):
        for chemin in [c for d in dossiers for c in sorted(glob.glob(os.path.join(d, "hms_%s_%s.json" % (langue, modele))))]:
            try:
                with open(chemin, encoding="utf-8") as fichier:
                    donnees = json.load(fichier).get("data") or {}
            except (OSError, ValueError, AttributeError):
                continue
            for groupe in ("device_hms", "device_error"):
                for liste in (donnees.get(groupe) or {}).values():
                    for x in liste if isinstance(liste, list) else []:
                        if isinstance(x, dict) and x.get("ecode") and x.get("intro"):
                            textes.setdefault(str(x["ecode"]).upper(), " ".join(str(x["intro"]).split()))
        if textes:
            break
    TEXTES_ERREURS[modele] = textes
    return textes


def code_lisible(code):
    """« 0700_8011 », « 0300_0100_0001_0007 » : le code comme Bambu Studio l'écrit."""
    return "_".join(code[i:i + 4] for i in range(0, len(code), 4))


DONNEES = dossier_donnees()
PAGE = "BlokBambu.html"
PORT = 47815
PAUSE = 3  # secondes entre deux passages
BOBINE_NEUVE = 1000  # grammes
PRESQUE_VIDE = 100   # grammes : en dessous, la bobine est signalée (réglable sur la page)
TYPES = ["PLA", "PETG", "ABS", "ASA", "TPU", "PA", "PC"]
EXTERNE = "254"  # le numéro que l'imprimante donne à la bobine posée hors de l'AMS

# L'imprimante : Bambu Studio garde son numéro de série et son code d'accès ; un certificat de
# Bambu Lab (AC_BAMBU, en fin de fichier) permet de la reconnaître avant de lui présenter ce code.
STABILISATION = 20   # secondes sans changement avant d'annoncer une bobine chargée
REMPLACEMENT = 180   # secondes : une bobine retirée puis remise dans ce délai est une bobine changée
ATTENTE_STUDIO = 120  # secondes laissées à Bambu Studio pour écrire son fichier avant de dire l'impression lancée ailleurs

# Le dépôt public : la page y renvoie pour le partage, et y lit quelle version est parue.
SITE = "https://github.com/Blokstats/blokbambu"
DEPOT = "https://raw.githubusercontent.com/Blokstats/blokbambu/main/"
# Grammes par centimètre cube, pour estimer une longueur de fil d'après son poids.
DENSITES = {"PLA": 1.24, "PETG": 1.27, "ABS": 1.04, "ASA": 1.07, "TPU": 1.21, "PA": 1.14, "PC": 1.2}
ETATS_IMPRIMANTE = {
    "recherche": "Recherche de l'imprimante…",
    "connectee": "Imprimante connectée : une fenêtre vous prévient quand une bobine est chargée ou retirée.",
    "injoignable": "Imprimante injoignable (éteinte ?) : les alertes de bobine reprendront dès qu'elle répondra.",
    "bloquee": "macOS empêche le compteur de joindre l'imprimante : dans Réglages Système › Confidentialité et "
               "sécurité › Réseau local, autorisez « Python ».",
    "refusee": "L'imprimante refuse le code d'accès gardé par Bambu Studio : pas d'alerte de bobine.",
    "sans_code": "Bambu Studio n'a pas de code d'accès pour l'imprimante : pas d'alerte de bobine.",
}

# Couleurs de référence pour nommer un code hexadécimal.
NOMS = [
    ("Noir", (0, 0, 0)), ("Blanc", (255, 255, 255)), ("Gris", (128, 128, 128)),
    ("Gris clair", (200, 200, 200)), ("Rouge", (220, 30, 35)), ("Orange", (250, 140, 55)),
    ("Jaune", (255, 212, 0)), ("Vert", (10, 190, 60)), ("Vert foncé", (0, 100, 50)),
    ("Bleu ciel", (118, 217, 244)), ("Bleu", (20, 90, 220)), ("Bleu marine", (15, 30, 90)),
    ("Violet", (130, 60, 200)), ("Rose", (255, 0, 202)), ("Marron", (120, 75, 40)),
    ("Beige", (225, 205, 170)),
]

verrou = threading.Lock()  # le journal et les bobines sont écrits par deux fils d'exécution


def racines_bambu():
    """Les dossiers où Bambu Studio peut poser son dossier de travail bamboo_model."""
    temporaire = tempfile.gettempdir()
    if MAC:
        try:
            temporaire = subprocess.check_output(["getconf", "DARWIN_USER_TEMP_DIR"], text=True).strip()
        except Exception:
            pass
    # Quand le disque temporaire est plein, Bambu Studio se replie sur le dossier tmp de ses réglages.
    racines = [temporaire] + [os.path.join(d, "tmp") for d in dossiers_bambu()]
    if not MAC and not WINDOWS:
        partage = os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()
        racines += ["/tmp", os.path.join(partage, ".flatpak", "com.bambulab.BambuStudio", "tmp")]
    return list(dict.fromkeys(racines))


def couleur_propre(code):
    """#RRGGBBAA ou #rrggbb → #RRGGBB."""
    code = (code or "").strip().upper()
    if not code.startswith("#"):
        code = "#" + code
    return code[:7] if len(code) >= 7 else "#808080"


def nom_couleur(code):
    try:
        r, v, b = (int(code[i:i + 2], 16) for i in (1, 3, 5))
    except ValueError:
        return "Couleur"
    return min(NOMS, key=lambda n: (n[1][0] - r) ** 2 + (n[1][1] - v) ** 2 + (n[1][2] - b) ** 2)[0]


# ---------------------------------------------------------------- lecture d'un envoi

def lire_envoi(chemin):
    """Rend {plateaux, secondes, fils, objets} pour un fichier envoyé à l'imprimante, ou None."""
    with zipfile.ZipFile(chemin) as z:
        noms = set(z.namelist())
        if "Metadata/slice_info.config" not in noms:
            return None
        racine = ET.fromstring(z.read("Metadata/slice_info.config"))
        reglages = {}
        if "Metadata/project_settings.config" in noms:
            try:
                reglages = json.loads(z.read("Metadata/project_settings.config"))
            except ValueError:
                pass
    return envoi_de(racine, noms, reglages.get("filament_settings_id") or [])


def envoi_de(racine, noms, presets, plateau=None):
    """Ce que dit le résumé de tranchage (slice_info) d'un fichier d'impression dont on connaît le contenu."""
    plateaux = []
    for p in racine.findall("plate"):
        meta = {m.get("key"): m.get("value") for m in p.findall("metadata")}
        plateaux.append((meta, p))
    # Un projet à plusieurs plateaux peut décrire des plateaux déjà tranchés mais non envoyés :
    # seuls ceux dont le g-code est dans le fichier partent à l'imprimante.
    envoyes = [(m, p) for m, p in plateaux if "Metadata/plate_%s.gcode" % m.get("index") in noms]
    if envoyes:
        plateaux = envoyes
    if plateau and any(m.get("index") == str(plateau) for m, _ in plateaux):
        plateaux = [(m, p) for m, p in plateaux if m.get("index") == str(plateau)]

    fils, indices, objets, secondes = {}, [], [], 0
    for meta, p in plateaux:
        indices.append(int(meta.get("index") or 0))
        secondes += int(float(meta.get("prediction") or 0))
        objets += [o.get("name") for o in p.findall("object") if o.get("name")]
        for f in p.findall("filament"):
            g = float(f.get("used_g") or 0)
            if g <= 0:
                continue
            numero = int(f.get("id") or 0)
            nom = presets[numero - 1].split(" @")[0] if 0 < numero <= len(presets) else ""
            cle = (couleur_propre(f.get("color")), f.get("type") or "", nom)
            fil = fils.setdefault(cle, {"couleur": cle[0], "type": cle[1], "nom": nom, "g": 0.0, "m": 0.0, "numero": numero})
            fil["g"] = round(fil["g"] + g, 2)
            fil["m"] = round(fil["m"] + float(f.get("used_m") or 0), 2)
    if not fils:
        return None
    return {"plateaux": sorted(indices), "secondes": secondes, "objets": objets,
            "fils": sorted(fils.values(), key=lambda f: -f["g"])}


def nom_projet(session, objets):
    try:
        with open(os.path.join(session, "origin.txt"), encoding="utf-8", errors="replace") as f:
            origine = f.read().strip()
    except OSError:
        origine = ""
    if origine:
        return os.path.splitext(os.path.basename(origine))[0], origine
    return (objets[0] if objets else "Projet sans nom"), ""


# ---------------------------------------------------------------- fichiers de données

def lire(donnees, nom, defaut):
    try:
        with open(os.path.join(donnees, nom), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return defaut


def ecrire(chemin, texte):
    provisoire = chemin + ".tmp"
    with open(provisoire, "w", encoding="utf-8") as f:
        f.write(texte)
    os.replace(provisoire, chemin)


def ecrire_json(donnees, nom, valeur):
    ecrire(os.path.join(donnees, nom), json.dumps(valeur, ensure_ascii=False, indent=1))


def lire_journal(donnees):
    return lire(donnees, "impressions.json", [])


def lire_bobines(donnees):
    """{identifiant: {couleur, type, marque, poids, prix, reserve, depuis, emplacement}} — les bobines suivies."""
    b = lire(donnees, "bobines.json", {})
    return {i: fiche for i, fiche in b.items() if isinstance(fiche, dict)} if isinstance(b, dict) else {}


def reprendre_anciennes_donnees(donnees):
    """Avant, une seule bobine par couleur et type de fil, désignée par eux. Chacune reçoit un
    identifiant, noté sur les fils des impressions qu'elle a fournies : plusieurs bobines de la
    même couleur peuvent alors se suivre côte à côte."""
    with verrou:
        bobines = lire(donnees, "bobines.json", {})
        if not isinstance(bobines, dict) or not any("|" in cle for cle in bobines):
            return False
        idents = {cle: hashlib.sha1(cle.encode("utf-8")).hexdigest()[:8] for cle in bobines if "|" in cle}
        journal = lire_journal(donnees)
        for entree in journal:
            for fil in entree.get("fils", []):
                cle = cle_fil(fil)
                if cle in idents and "bobine" not in fil and entree["date"] >= bobines[cle].get("depuis", ""):
                    fil["bobine"] = idents[cle]
        ecrire_json(donnees, "impressions.json", journal)
        ecrire_json(donnees, "bobines.json", {idents.get(cle, cle): b for cle, b in bobines.items()})
        return True


def relever_couleurs(dossier, donnees):
    """Note les couleurs réglées dans les projets Bambu d'un dossier, pour les proposer à l'ajout."""
    vues = {}
    for d, _, fichiers in os.walk(dossier):
        for f in fichiers:
            if not f.lower().endswith(".3mf"):
                continue
            try:
                with zipfile.ZipFile(os.path.join(d, f)) as z:
                    r = json.loads(z.read("Metadata/project_settings.config"))
            except (OSError, KeyError, ValueError, zipfile.BadZipFile):
                continue
            types, presets = r.get("filament_type") or [], r.get("filament_settings_id") or []
            for i, c in enumerate(r.get("filament_colour") or []):
                cle = "%s|%s" % (couleur_propre(c), types[i] if i < len(types) else "PLA")
                noms = vues.setdefault(cle, [])
                nom = presets[i].split(" @")[0] if i < len(presets) else ""
                if nom and nom not in noms:
                    noms.append(nom)
    ecrire_json(donnees, "couleurs.json", vues)
    return vues


# ---------------------------------------------------------------- impressions comptées

def identifiant(entree):
    return hashlib.sha1(entree["cle"].encode("utf-8")).hexdigest()[:12]


def part(entree):
    """La part réellement imprimée : 1 pour une impression menée au bout, moins si elle a été arrêtée."""
    try:
        return max(min(float(entree.get("part", 1)), 1.0), 0.0)
    except (TypeError, ValueError):
        return 1.0


def cle_fil(f):
    return "%s|%s" % (f["couleur"], f["type"])


def comptees(journal):
    return [e for e in journal if not e.get("supprimee")]


def troncons(entree, f):
    """Ce que chaque bobine a fourni d'un fil : [(bobine, part de l'impression, prix au kilo)].

    Une bobine changée en cours d'impression laisse la suite à celle qui prend le relais (`relais` :
    à partir de telle part de l'impression, telle bobine). Seule la part imprimée compte.
    """
    p = part(entree)
    bornes = [(0.0, f.get("bobine"), f.get("prix_kg"))] + [
        (r["a"], r.get("bobine"), r.get("prix_kg")) for r in sorted(f.get("relais") or [], key=lambda r: r["a"])]
    sortie = []
    for rang, (debut, bobine, prix) in enumerate(bornes):
        fin = bornes[rang + 1][0] if rang + 1 < len(bornes) else 1.0
        if min(fin, p) > debut:
            sortie.append((bobine, min(fin, p) - debut, prix))
    return sortie


def restes(journal, bobines):
    """Ce qu'il reste sur chaque bobine suivie, d'après les impressions qui s'en sont servies."""
    r = {ident: float(b["poids"]) for ident, b in bobines.items()}
    for entree in comptees(journal):
        for f in entree["fils"]:
            for bobine, morceau, _ in troncons(entree, f):
                if bobine in r:
                    r[bobine] -= f["g"] * morceau
    return r


def poser_prix(entree, bobines):
    """Note sur chaque fil le prix au kilo de sa bobine : le coût d'une impression ne bouge plus ensuite."""
    for f in entree["fils"]:
        for porteur in [f] + list(f.get("relais") or []):
            if not porteur.get("bobine"):
                porteur.pop("prix_kg", None)
            elif porteur["bobine"] in bobines:  # une bobine remplacée depuis : le prix noté alors est gardé
                if bobines[porteur["bobine"]].get("prix"):
                    porteur["prix_kg"] = bobines[porteur["bobine"]]["prix"]
                else:
                    porteur.pop("prix_kg", None)


def cout(entree):
    """Coût du fil d'une impression, ou None si aucun de ses fils n'a de prix."""
    prix = [f["g"] * morceau * prix_kg / 1000 for f in entree["fils"]
            for _, morceau, prix_kg in troncons(entree, f) if prix_kg]
    return round(sum(prix), 2) if prix else None


def temps(entree):
    """Le temps d'impression, en secondes : celui prévu par Bambu Studio pour la part faite, sinon
    celui observé sur l'imprimante, pauses déduites."""
    if entree.get("secondes"):
        return entree["secondes"] * part(entree)
    return float(entree.get("duree") or 0)


def metres(grammes, type_fil):
    """Longueur estimée d'un fil de 1,75 mm d'après son poids, quand Bambu Studio ne l'a pas donnée."""
    densite = DENSITES.get(re.split(r"[^A-Z]", type_fil.upper())[0], DENSITES["PLA"])
    return round(grammes / (densite * 2.405), 2)


def poids_texte(g):
    if g >= 1000:
        return ("%.2f" % (g / 1000)).rstrip("0").rstrip(".").replace(".", ",") + " kg"
    return "%d g" % round(g)


def duree_texte(secondes):
    heures, minutes = int(secondes // 3600), int(secondes % 3600 // 60)
    return "%d h %02d" % (heures, minutes) if heures else "%d min" % max(minutes, 1)


def ecart(couleur_a, couleur_b):
    try:
        a, b = ([int(c[i:i + 2], 16) for i in (1, 3, 5)] for c in (couleur_a, couleur_b))
    except ValueError:
        return 10 ** 9
    return sum((x - y) ** 2 for x, y in zip(a, b))


def attribuer_bacs(entree, utilises, bobines, fin):
    """Rattache les fils d'une impression aux bobines qui ont vraiment fourni.

    `utilises` : ce qu'il y avait dans les emplacements vus en service ({couleur, type, bobine, bac}).
    L'imprimante ne dit pas quel fil du projet va sur quel emplacement : avec plusieurs fils, on
    attend la fin et on prend, pour chacun, l'emplacement utilisé de la couleur la plus proche.
    Une impression lancée ailleurs que depuis Bambu Studio n'a pas de fils connus : ce sont ceux
    des emplacements utilisés, grammes à compléter.
    """
    if entree.get("a_completer"):
        fils = []
        for u in utilises:
            f = {"couleur": u["couleur"], "type": u["type"], "nom": "", "g": 0.0, "m": 0.0, "bac": u["bac"]}
            if u.get("bobine"):
                f["bobine"] = u["bobine"]
            fils.append(f)
        if fils == entree["fils"]:
            return False
        entree["fils"] = fils
        return True
    fils = entree["fils"]
    if not utilises or (len(fils) > 1 and (not fin or len(utilises) < len(fils))):
        return False
    change = False
    for f in fils:
        if f.get("bobine_choisie"):  # la personne a dit quelle bobine a servi : on n'y revient pas
            continue
        origine = f.get("projet") or {"couleur": f["couleur"], "type": f["type"]}
        if len(fils) == 1:
            choix = utilises[0]
        else:
            memes = [u for u in utilises if u["type"] == origine["type"]] or utilises
            choix = min(memes, key=lambda u: ecart(u["couleur"], origine["couleur"]))
        if choix.get("bac") and f.get("bac") != choix["bac"]:
            f["bac"] = choix["bac"]
            change = True
        if (choix["couleur"], choix["type"], choix.get("bobine")) != (f["couleur"], f["type"], f.get("bobine")):
            if (choix["couleur"], choix["type"]) != (origine["couleur"], origine["type"]):
                f["projet"] = origine
            f["couleur"], f["type"] = choix["couleur"], choix["type"]
            if choix.get("bobine"):
                f["bobine"] = choix["bobine"]
            else:
                f.pop("bobine", None)
            change = True
    if change:
        poser_prix(entree, bobines)
    return appliquer_changements(entree, bobines) or change


def dernier_occupant(entree, bac):
    """La bobine qui alimente cet emplacement pour cette impression, d'après ce qui en a été noté."""
    for c in reversed(entree.get("changements") or []):
        if c.get("par") == bac:
            return {"bobine": c.get("bobine"), "cle": c.get("cle")}
    return ((entree.get("suivi") or {}).get("premiers") or {}).get(bac)


def noter_coupure(entree, bac, percent):
    """Un emplacement qui alimentait l'impression vient de se vider : on retient à quel avancement."""
    suivi = entree.setdefault("suivi", {"premiers": {}, "coupures": {}})
    if bac not in suivi["premiers"] and not any(c.get("par") == bac for c in entree.get("changements") or []):
        return False
    suivi["coupures"][bac] = percent
    return True


def noter_occupant(entree, bac, occupant, percent, bobines):
    """Note la bobine qui alimente l'impression depuis cet emplacement, et celle qui prend le relais quand
    elle change en cours de route : bobine neuve à la place d'une bobine terminée, autre bobine posée là,
    ou, pour un fil unique, l'AMS qui passe sur un autre emplacement. Rend True si quelque chose est noté."""
    suivi = entree.setdefault("suivi", {"premiers": {}, "coupures": {}})
    premiers, coupures = suivi["premiers"], suivi["coupures"]
    changements = entree.setdefault("changements", [])
    nouvelle = (occupant or {}).get("bobine")
    if bac not in premiers:
        seul = len(entree["fils"]) == 1 and not entree.get("a_completer")
        if seul and premiers:
            source = entree["fils"][0].get("bac") or next(iter(premiers))
            changements.append({"bac": source, "par": bac, "a": round(percent / 100, 4), "bobine": nouvelle,
                                "cle": (occupant or {}).get("cle")})
        premiers[bac] = occupant
    else:
        avant = dernier_occupant(entree, bac)
        if nouvelle == (avant or {}).get("bobine"):
            return False
        if not avant and bac not in coupures:  # le fil de cet emplacement vient seulement d'être reconnu
            premiers[bac] = occupant
        elif nouvelle or bac in coupures:
            source = next((c["bac"] for c in changements if c.get("par") == bac), bac)
            changements.append({"bac": source, "par": bac, "a": round(coupures.pop(bac, percent) / 100, 4),
                                "bobine": nouvelle, "cle": (occupant or {}).get("cle")})
        else:
            return False
    if not changements:
        del entree["changements"]
    appliquer_changements(entree, bobines)
    return True


def appliquer_changements(entree, bobines):
    """Reporte sur chaque fil les bobines qui ont pris le relais sur son emplacement en cours d'impression."""
    change = False
    for f in entree["fils"]:
        if f.get("relais_choisis"):  # la personne l'a dit elle-même : on n'y revient pas
            continue
        relais = [{"a": c["a"], "bobine": c.get("bobine")} for c in entree.get("changements") or []
                  if f.get("bac") and c["bac"] == f["bac"] and c["a"] > 0]
        if relais != [{"a": r["a"], "bobine": r.get("bobine")} for r in f.get("relais") or []]:
            if relais:
                f["relais"] = relais
            else:
                f.pop("relais", None)
            change = True
    if change:
        poser_prix(entree, bobines)
    return change


# ---------------------------------------------------------------- imprimantes et emplacements gardés

def repere_de(serie):
    """Ce qui désigne une imprimante dans les fichiers : son numéro de série n'y est jamais écrit."""
    return hashlib.sha1(serie.encode("utf-8")).hexdigest()[:10]


def imprimantes_gardees(donnees):
    """La mémoire des imprimantes, par repère : rang, nom donné, emplacements vus au dernier contact."""
    m = lire(donnees, "imprimantes.json", {})
    return {r: fiche for r, fiche in m.items() if isinstance(fiche, dict)} if isinstance(m, dict) else {}


def suivies(memoire):
    """Les imprimantes suivies, dans l'ordre de leur rang : [(repère, fiche)]."""
    return sorted(((r, fiche) for r, fiche in memoire.items() if fiche.get("suivie", True) and not fiche.get("retiree")),
                  key=lambda x: x[1].get("numero", 0))


def nom_imprimante(memoire, repere):
    """Le nom donné à une imprimante ; sinon « Imprimante », avec son rang s'il y en a plusieurs."""
    fiche = memoire.get(repere)
    if fiche is None:
        return "Autre imprimante"
    seule = [r for r, _ in suivies(memoire)] == [repere]
    return fiche.get("nom") or ("Imprimante" if seule else "Imprimante %d" % fiche.get("numero", 0))


def imprimante_de(entree, memoire):
    """Le repère de l'imprimante qui a fait une impression, ou None si on ne le sait pas."""
    choisie = entree.get("imprimante")
    if isinstance(choisie, str):
        return choisie or None
    if entree.get("tache"):
        return entree["tache"].partition(":")[0]
    if isinstance(choisie, int):  # avant, c'était le rang de l'imprimante
        return next((r for r, fiche in memoire.items() if fiche.get("numero") == choisie), None)
    return None


def rattacher(donnees):
    """Tant qu'une seule imprimante est suivie, les impressions dont on ignore l'imprimante sont les siennes."""
    memoire = imprimantes_gardees(donnees)
    seules = [r for r, _ in suivies(memoire)]
    if len(seules) != 1:
        return
    with verrou:
        journal = lire_journal(donnees)
        sans = [e for e in journal if imprimante_de(e, memoire) is None]
        for entree in sans:
            entree["imprimante"] = seules[0]
        if sans:
            ecrire_json(donnees, "impressions.json", journal)


def ordre_emplacement(cle):
    """A1, A2… puis la bobine externe."""
    if cle == EXTERNE:
        return 99
    unite, _, bac = cle.partition("-")
    try:
        return int(unite) * 4 + int(bac)
    except ValueError:
        return 98


def emplacements_connus(imprimantes):
    """Les emplacements des imprimantes données ([(repère, fiche)]), tels que gardés au dernier contact.

    Sans imprimante suivie, un AMS et la bobine externe restent proposés : on peut ranger ses
    bobines par emplacement même sans liaison.
    """
    liste = []
    for position, (repere, fiche) in enumerate(imprimantes or [("", {})]):
        connus = dict(fiche.get("emplacements") or {})
        for cle in ["0-%d" % bac for bac in range(4)] + [EXTERNE]:
            connus.setdefault(cle, {})
        for cle in sorted(connus, key=ordre_emplacement):
            e = connus[cle]
            poids_puce, remain = e.get("poids_puce") or 0, e.get("remain")
            lu = e.get("puce") and poids_puce > 0 and remain is not None and 0 <= remain <= 100
            liste.append(dict(e, id="%s:%s" % (repere, cle), imprimante=repere, bac=cle, nom=nom_emplacement(cle),
                              rang=position * 100 + ordre_emplacement(cle),
                              reste_puce=round(poids_puce * remain / 100, 1) if lu else None))
    return liste


def place_choisie(bobine, par_id):
    """L'emplacement où la personne a dit avoir posé cette bobine, s'il existe toujours."""
    choix = bobine.get("emplacement") or ""
    if choix in par_id:
        return choix
    repere, _, cle = choix.partition(":")
    if choix and not repere:  # choisi quand aucune imprimante n'était suivie : il vaut pour la première
        return next((i for i in par_id if i.endswith(":" + cle)), None)
    return None


def occupation(places, bobines, annonces_sures=True):
    """Qui occupe chaque emplacement : {id: {cle, bobine, choisi}}.

    La bobine que la personne y a posée sur la page fait foi. Sinon c'est le fil que l'imprimante
    annonce : la bobine suivie de cette couleur et de ce type qui n'est pas posée ailleurs (la plus
    ancienne s'il y en a plusieurs), ou aucune — un fil chargé mais pas suivi.
    """
    par_id = {p["id"]: p for p in places}
    posees = {}
    for ident, b in bobines.items():
        choix = place_choisie(b, par_id)
        if choix:
            posees[choix] = ident
    libres = {}
    for ident, b in sorted(bobines.items(), key=lambda x: x[1].get("depuis", "")):
        if ident not in posees.values():
            libres.setdefault(cle_fil(b), []).append(ident)
    occupe = {}
    for p in places:
        present = p.get("present") if annonces_sures else p.get("present") is not False
        if p["id"] in posees:
            occupe[p["id"]] = {"cle": cle_fil(bobines[posees[p["id"]]]), "bobine": posees[p["id"]], "choisi": True}
        elif present and p.get("couleur") and p.get("type"):
            cle = "%s|%s" % (p["couleur"], p["type"])
            memes = libres.get(cle) or []
            occupe[p["id"]] = {"cle": cle, "bobine": memes.pop(0) if memes else None, "choisi": False}
    return occupe


def bobine_probable(cle, bobines, occupe):
    """La bobine suivie qui fournit sans doute ce fil : la seule de cette couleur et de ce type,
    sinon celle qui est sur l'imprimante, sinon la plus ancienne."""
    memes = [i for i, b in sorted(bobines.items(), key=lambda x: x[1].get("depuis", "")) if cle_fil(b) == cle]
    en_place = [o["bobine"] for o in occupe.values() if o["bobine"] in memes]
    return (en_place or memes or [None])[0]


def poser_bobine(bobines, ident, emplacement):
    """Pose une bobine à un emplacement (ou « automatique » : vide). Celle qui s'y trouvait le quitte."""
    for b in bobines.values():
        if emplacement and b.get("emplacement") == emplacement:
            del b["emplacement"]
    if emplacement:
        bobines[ident]["emplacement"] = emplacement
    else:
        bobines[ident].pop("emplacement", None)


# ---------------------------------------------------------------- fenêtres d'alerte

def demander(texte, rappel=""):
    """Affiche la fenêtre d'alerte. Rend True si la personne veut ouvrir la page."""
    if MAC:
        # La fenêtre aux couleurs de la page, compilée à l'installation ; à défaut, celle du système.
        dessinee = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alerte")
        if os.access(dessinee, os.X_OK):
            try:
                code = subprocess.run([dessinee, texte, rappel]).returncode
                if code in (0, 1):
                    return code == 0
            except OSError:
                pass
        lignes = " & return & ".join('"%s"' % l.replace("\\", "\\\\").replace('"', '\\"')
                                     for l in texte.split("\n") + (["", rappel] if rappel else []))
        r = subprocess.run(["/usr/bin/osascript", "-e", "tell me to activate", "-e",
                            'display dialog %s with title "BlokBambu" buttons {"Plus tard", "Ouvrir la page"} '
                            'default button 2' % lignes], capture_output=True, text=True)
        return "Ouvrir la page" in r.stdout
    annonce = texte + ("\n\n" + rappel if rappel else "")
    if WINDOWS:
        import ctypes
        # Oui / Non, icône d'information, au premier plan, par-dessus les autres fenêtres.
        return ctypes.windll.user32.MessageBoxW(None, annonce + "\n\nOuvrir la page maintenant ?",
                                                "BlokBambu", 0x4 | 0x40 | 0x10000 | 0x40000) == 6
    for commande in (["zenity", "--question", "--title=BlokBambu", "--text=" + annonce,
                      "--ok-label=Ouvrir la page", "--cancel-label=Plus tard"],
                     ["kdialog", "--title", "BlokBambu", "--yesno", annonce,
                      "--yes-label", "Ouvrir la page", "--no-label", "Plus tard"]):
        try:
            return subprocess.run(commande, capture_output=True).returncode == 0
        except OSError:
            continue
    try:
        subprocess.run(["notify-send", "BlokBambu", annonce], capture_output=True)
    except OSError:
        pass
    return False


def ouvrir_page(adresse):
    if MAC:
        subprocess.run(["/usr/bin/open", adresse])
    else:
        import webbrowser
        webbrowser.open(adresse)


verrou_alertes = threading.Lock()


def lire_alertes(donnees):
    return [a for a in lire(donnees, "alertes.json", []) if isinstance(a, dict)]


class Fenetres:
    """Les alertes : notées pour la page, et montrées dans une fenêtre du système, une seule à la fois."""

    def __init__(self, port, donnees, essai=None):
        self.port, self.donnees, self.essai, self.une_seule = port, donnees, essai, threading.Lock()

    def montrer(self, texte, rappel=""):
        with verrou_alertes:  # la page les affiche à son tour, jusqu'à ce qu'on les ferme
            alertes = lire_alertes(self.donnees) + [{"id": secrets.token_hex(4), "texte": texte,
                                                     "date": datetime.now().isoformat(timespec="seconds")}]
            ecrire_json(self.donnees, "alertes.json", alertes[-20:])
        threading.Thread(target=self.afficher, args=(texte, rappel), daemon=True).start()

    def afficher(self, texte, rappel):
        print("fenêtre :", texte.replace("\n", " "), flush=True)
        if self.essai:
            with open(self.essai, "a", encoding="utf-8") as f:
                f.write(texte.replace("\n", " | ") + "\n")
            return
        with self.une_seule:
            ouvrir = demander(texte, rappel)
        if ouvrir:
            ouvrir_page("http://127.0.0.1:%d/" % self.port)


# ---------------------------------------------------------------- surveillance

class Veilleur:
    def __init__(self, racines, donnees, fenetres):
        self.racines, self.donnees, self.fenetres = racines, donnees, fenetres
        self.observes = {}  # chemin → (date de modification, taille) au passage précédent
        self.rejetes = {}   # chemin → même repère, pour un fichier illisible

    def passage(self):
        """Note les nouveaux envois. Rend le nombre d'impressions ajoutées."""
        nouvelles = []
        chemins = [c for r in self.racines
                   for c in glob.glob(os.path.join(r, "bamboo_model", "*", "*", "Metadata", ".*.3mf"))]
        for chemin in chemins:
            if chemin.endswith("_config.3mf"):
                continue
            try:
                s = os.stat(chemin)
            except OSError:
                continue
            repere = (s.st_mtime_ns, s.st_size)
            stable = self.observes.get(chemin) == repere and time.time() - s.st_mtime >= 2
            self.observes[chemin] = repere
            if not stable or self.rejetes.get(chemin) == repere:
                continue  # encore en cours d'écriture, ou déjà traité
            cle = "%s|%d|%d" % (chemin, s.st_mtime_ns, s.st_size)
            if any(e.get("cle") == cle for e in lire_journal(self.donnees)):
                self.rejetes[chemin] = repere
                continue
            try:
                envoi = lire_envoi(chemin)
            except OSError:
                continue  # encore tenu par Bambu Studio (Windows) : on y revient au passage suivant
            except (zipfile.BadZipFile, ET.ParseError, KeyError, ValueError) as e:
                print("illisible :", chemin, e, flush=True)
                envoi = None
            self.rejetes[chemin] = repere
            if not envoi:
                continue
            session = os.path.dirname(os.path.dirname(chemin))
            projet, fichier = nom_projet(session, envoi.pop("objets"))
            entree = {"cle": cle, "date": datetime.fromtimestamp(s.st_mtime).isoformat(timespec="seconds"),
                      "projet": projet, "fichier": fichier, "etat": "lancee"}
            entree.update(envoi)
            nouvelles.append(entree)
            print("impression comptée :", projet, envoi["fils"], flush=True)
        if nouvelles:
            with verrou:
                journal, bobines = lire_journal(self.donnees), lire_bobines(self.donnees)
                reste = restes(journal, bobines)
                occupe = occupation(emplacements_connus(suivies(imprimantes_gardees(self.donnees))), bobines)
                for entree in nouvelles:
                    for f in entree["fils"]:
                        choix = bobine_probable(cle_fil(f), bobines, occupe)
                        if choix:
                            f["bobine"] = choix
                    poser_prix(entree, bobines)
                    self.alerter(entree, bobines, reste)
                journal += nouvelles
                journal.sort(key=lambda e: e["date"])
                ecrire_json(self.donnees, "impressions.json", journal)
        return len(nouvelles)

    def alerter(self, entree, bobines, reste):
        """Prévient si l'impression demande plus de fil qu'il n'en reste, ou vide la dernière bobine."""
        lignes, seuil = [], lire_reglages(self.donnees)["seuil"]
        for f in entree["fils"]:
            cle = f.get("bobine")
            if cle not in bobines:
                continue
            avant = reste[cle]
            reste[cle] = avant - f["g"]
            nom = " ".join(x for x in (nom_couleur(f["couleur"]), bobines[cle].get("marque")) if x)
            if f["g"] > avant:
                lignes.append("%s : il faut %s, il en reste environ %s sur la bobine."
                              % (nom, poids_texte(f["g"]), poids_texte(max(avant, 0))))
            elif avant >= seuil > reste[cle] and bobines[cle].get("reserve") == 0:
                lignes.append("%s : bientôt vide, et plus de bobine en réserve." % nom)
        if lignes:
            self.fenetres.montrer("« %s » vient d'être lancée.\n%s" % (entree["projet"], "\n".join(lignes)))


# ---------------------------------------------------------------- page

ICI = os.path.dirname(os.path.abspath(__file__))
# La page et la police de ses titres sont des fichiers posés à côté de ce programme.
FICHIERS_PAGE = {"/": ("page.html", "text/html; charset=utf-8", "no-store"),
                 "/police.woff2": ("police.woff2", "font/woff2", "max-age=86400")}

MARQUES = ["Bambu Lab", "eSUN", "Polymaker", "Sunlu", "Elegoo", "Creality", "Anycubic", "Overture", "Prusament",
           "Eryone", "Jayo", "Kingroon", "Geeetech", "R3D", "Rosa3D", "ColorFabb", "Fillamentum", "Arianeplast",
           "Francofil"]
# Ce que le nom d'un fil dans Bambu Studio dit de sa marque (« eSUN PLA+ », « PolyTerra PLA »…).
INDICES_MARQUE = [("bambu", "Bambu Lab"), ("esun", "eSUN"), ("poly", "Polymaker"), ("panchroma", "Polymaker"),
                  ("sunlu", "Sunlu"), ("elegoo", "Elegoo"), ("creality", "Creality"), ("anycubic", "Anycubic"),
                  ("overture", "Overture"), ("prusament", "Prusament"), ("eryone", "Eryone"), ("jayo", "Jayo"),
                  ("kingroon", "Kingroon"), ("geeetech", "Geeetech"), ("colorfabb", "ColorFabb"),
                  ("fillamentum", "Fillamentum"), ("rosa3d", "Rosa3D"), ("r3d", "R3D")]
# Ce que le code de fil annoncé par l'imprimante dit de la marque.
CODES_MARQUE = {"GFL00": "Polymaker", "GFL01": "Polymaker", "GFL03": "eSUN", "GFL04": "Overture", "GFL05": "Overture"}
ETATS_COURTS = {"recherche": "recherchée", "connectee": "connectée", "injoignable": "injoignable",
                "bloquee": "bloquée", "refusee": "refusée", "sans_code": "sans code"}
EMPLACEMENT = r"(?:(?:[0-9a-f]{10})?:(?:254|\d{1,2}-\d))?"  # « repère:emplacement », ou vide pour « automatique »
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
        "novembre", "décembre"]

# Le raccourci posé dans un dossier (Mac) : il bascule vers la page servie par le compteur.
RACCOURCI = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>BlokBambu</title>
<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#090909;
color:#e8e4dc;font:15px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;text-align:center}
b{display:block;font-size:13px;letter-spacing:.1em;text-transform:uppercase;color:#ffb400;margin-bottom:10px}
a{color:#fff}</style></head><body><p><b>BlokBambu</b>
La page s'ouvre sur <a href="http://127.0.0.1:PORT/">http://127.0.0.1:PORT/</a>.<br>
Si rien ne se passe, BlokBambu n'est pas lancé sur cet ordinateur.</p>
<script src="http://127.0.0.1:PORT/saut.js"></script></body></html>
"""

# La page que voit un téléphone avant d'avoir donné le code d'accès.
PAGE_CODE = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>BlokBambu</title>
<style>*{box-sizing:border-box}body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#090909;color:#e8e4dc;font:16px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif}
form{width:min(340px,calc(100vw - 32px));background:#111;border:1px solid rgba(255,255,255,.13);border-radius:14px;
padding:26px 24px}b{display:block;font-size:13px;letter-spacing:.1em;text-transform:uppercase;color:#ffb400;
margin-bottom:6px}p{margin:0 0 18px;color:#b0aea8;font-size:14px}
input{width:100%;font-size:22px;letter-spacing:.3em;text-align:center;padding:12px;border-radius:9px;
border:1px solid rgba(255,255,255,.13);background:#161616;color:#fff;outline:none}
button{width:100%;margin-top:14px;font-size:16px;font-weight:700;padding:12px;border:0;border-radius:9px;
background:#ffb400;color:#000}i{display:block;min-height:20px;margin-top:12px;color:#EF00CA;font-style:normal;
font-size:14px}</style></head><body>
<form method="post" action="/code"><b>BlokBambu</b><p>Entrez le code affiché dans les réglages de BlokBambu, sur
l'ordinateur.</p><input name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="6" autofocus required>
<button>Entrer</button><i>MESSAGE</i></form></body></html>
"""


def cle_tri(cle):
    couleur, _, type_fil = cle.partition("|")
    return (nom_couleur(couleur), couleur, type_fil)


def marque_devinee(noms):
    for nom in noms:
        for indice, marque in INDICES_MARQUE:
            if nom.lower().startswith(indice):
                return marque
    return ""


def marques_proposees(donnees):
    """Les marques de base, puis celles ajoutées à la main."""
    ajoutees = [m for m in lire(donnees, "marques.json", []) if isinstance(m, str)]
    return MARQUES + [m for m in ajoutees if m not in MARQUES]


def lire_reglages(donnees):
    r = lire(donnees, "reglages.json", {})
    r = r if isinstance(r, dict) else {}
    try:
        seuil = max(min(int(r.get("seuil", PRESQUE_VIDE)), 5000), 0)
    except (TypeError, ValueError):
        seuil = PRESQUE_VIDE
    return {"reseau": bool(r.get("reseau")), "code": str(r.get("code") or ""), "secret": str(r.get("secret") or ""),
            "seuil": seuil, "fin": r.get("fin", True) is not False, "carte": bool(r.get("carte"))}


# ----- mise à jour : la page dit qu'une version plus récente est parue ; rien ne s'installe tout seul

def lire_version(texte):
    """(numéro, ce qui change) d'un fichier version.txt : le numéro en première ligne, les nouveautés ensuite."""
    lignes = [ligne.strip() for ligne in texte.strip().splitlines()]
    try:
        return int(lignes[0]), " ".join(ligne for ligne in lignes[1:] if ligne)[:300]
    except (IndexError, ValueError):
        return 0, ""


def version_installee():
    try:
        with open(os.path.join(ICI, "version.txt"), encoding="utf-8") as fichier:
            return lire_version(fichier.read())[0]
    except OSError:
        return 0


def ligne_installation():
    """La ligne à relancer pour mettre à jour, celle du README pour ce système."""
    if MAC:
        return "curl -fsSL %sinstaller.sh | zsh" % DEPOT
    if WINDOWS:
        return "irm %sinstaller.ps1 | iex" % DEPOT
    return "curl -fsSL %sinstaller-linux.sh | sh" % DEPOT


class Nouveautes(threading.Thread):
    """Regarde une fois par jour si le dépôt porte une version plus récente. Rien n'est envoyé, rien n'est installé."""

    def __init__(self, adresse, attente=8):
        super().__init__(daemon=True)
        self.adresse, self.attente, self.ici, self.parue = adresse, attente, version_installee(), None

    def lire(self):
        try:
            with urllib.request.urlopen(self.adresse, timeout=15) as reponse:
                return reponse.read(4000).decode("utf-8", "replace")
        except Exception:
            pass
        try:  # certains Python (macOS) n'ont pas les certificats du système : curl, lui, les a
            sortie = subprocess.run(["curl", "-fsSL", "--max-time", "15", self.adresse], capture_output=True,
                                    timeout=20, **SANS_FENETRE).stdout
            return sortie[:4000].decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError):
            return ""

    def run(self):
        time.sleep(self.attente)
        while True:
            numero, quoi = lire_version(self.lire())
            self.parue = {"version": numero, "quoi": quoi} if numero > self.ici else None
            fin = time.time() + (24 * 3600 if numero else 3600)
            while time.time() < fin:
                time.sleep(60)

    def annonce(self):
        parue = self.parue
        return parue and dict(parue, commande=ligne_installation(),
                              ou="le Terminal" if MAC else "PowerShell" if WINDOWS else "un terminal")


def jeton_reseau(reglages):
    return hashlib.sha256(("%s:%s" % (reglages["secret"], reglages["code"])).encode("utf-8")).hexdigest()


def adresse_locale():
    """L'adresse de cet ordinateur sur le réseau local, ou None. Aucun paquet n'est envoyé."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            adresse = s.getsockname()[0]
    except OSError:
        return None
    return adresse if ipaddress.ip_address(adresse).is_private and not adresse.startswith("127.") else None


def etat_page(donnees, parc=None, local=True, port=PORT, nouveautes=None):
    """Tout ce que la page affiche, calculé à la demande."""
    journal, bobines = comptees(lire_journal(donnees)), lire_bobines(donnees)
    connues = lire(donnees, "couleurs.json", {})
    reglages = lire_reglages(donnees)
    memoire = imprimantes_gardees(donnees)
    ecoutees = suivies(memoire) if parc else []
    vivantes = parc.etats() if parc else {}
    reste = restes(journal, bobines)
    maintenant = datetime.now()
    ce_mois = maintenant.strftime("%Y-%m")

    def compte():
        return {"impressions": 0, "g": 0.0, "cout": 0.0, "prix": False, "secondes": 0.0}

    def chiffres(c):
        return {"impressions": c["impressions"], "g": round(c["g"], 2), "secondes": round(c["secondes"]),
                "cout": round(c["cout"], 2) if c["prix"] else None}

    totaux, mois, machines, imprimees = {}, {}, {}, set()
    for entree in journal:
        p, prix, duree = part(entree), cout(entree), temps(entree)
        grammes = sum(f["g"] for f in entree["fils"]) * p
        machine = machines.setdefault(imprimante_de(entree, memoire), {"total": compte(), "mois": compte()})
        comptes = [mois.setdefault(entree["date"][:7], compte()), machine["total"]]
        if entree["date"][:7] == ce_mois:
            comptes.append(machine["mois"])
        for c in comptes:
            c["impressions"] += 1
            c["g"] += grammes
            c["secondes"] += duree
            if prix is not None:
                c["cout"], c["prix"] = c["cout"] + prix, True
        for f in entree["fils"]:
            if not f["g"]:  # impression à compléter : ses grammes ne sont pas encore connus
                continue
            if entree.get("source") != "carte":
                imprimees.add(cle_fil(f))
            t = totaux.setdefault(cle_fil(f), {"noms": [], "g": 0.0, "m": 0.0, "n": 0})
            t["g"] += f["g"] * p
            t["m"] += f["m"] * p
            t["n"] += 1
            if f.get("nom") and f["nom"] not in t["noms"]:
                t["noms"].append(f["nom"])

    # Les imprimantes dont on parle : celles qu'on suit, puis celles qui ont fait des impressions.
    noms = {r: nom_imprimante(memoire, r) for r, _ in ecoutees}
    for r in machines:
        if r and r not in noms:
            noms[r] = nom_imprimante(memoire, r)
    plusieurs = len(ecoutees) > 1

    # Où est chaque bobine : là où on l'a posée sur la page, sinon là où l'imprimante annonce son fil.
    places = emplacements_connus(ecoutees)
    par_id = {p["id"]: p for p in places}
    occupe = occupation(places, bobines)
    lieux, sans_bobine, puces, codes = {}, {}, {}, {}
    for p in places:
        o = occupe.get(p["id"])
        if not o:
            continue
        qui = o["bobine"] or o["cle"]
        (lieux if o["bobine"] else sans_bobine).setdefault(qui, []).append(p)
        if not o["choisi"]:
            if p["reste_puce"] is not None:  # seules les bobines Bambu Lab portent une puce
                puces[qui] = (p["reste_puce"], p.get("poids_puce") or BOBINE_NEUVE)
            if p.get("indice"):
                codes[qui] = p["indice"]

    def fiche(cle, ou=()):
        couleur, _, type_fil = cle.partition("|")
        return {"cle": cle, "couleur": couleur, "nom": nom_couleur(couleur), "type": type_fil,
                "emplacements": [p["nom"] for p in ou], "rang": min([p["rang"] for p in ou] + [9999]),
                "imprimantes": list(dict.fromkeys(noms[p["imprimante"]] for p in ou)) if plusieurs else []}

    def marque_probable(cle, qui=None):
        if (qui or cle) in puces:
            return "Bambu Lab"
        return CODES_MARQUE.get(codes.get(qui or cle, "")) or marque_devinee(totaux[cle]["noms"] if cle in totaux
                                                                             else connues.get(cle, []))

    def jauge(valeur, plein):  # se lit par rapport à une bobine pleine, pas par rapport au poids saisi
        return {"reste": round(valeur, 2), "poids": plein, "bas": valeur < reglages["seuil"],
                "part": round(max(min(valeur / max(plein, BOBINE_NEUVE), 1), 0), 4)}

    cartes, reste_total, a_racheter = [], 0.0, []
    for ident, b in sorted(bobines.items(), key=lambda x: cle_tri(cle_fil(x[1])) + (x[1].get("depuis", ""),)):
        cle, choix = cle_fil(b), place_choisie(b, par_id)
        f = fiche(cle, lieux.get(ident, []))
        if ident in puces:  # la puce d'une bobine Bambu Lab dit ce qu'il reste : elle fait foi
            f.update(jauge(*puces[ident]), source="puce")
        else:
            f.update(jauge(reste[ident], b["poids"]))
        reste_total += max(f["reste"], 0)
        f.update(id=ident, suivie=True, marque=b.get("marque") or marque_probable(cle, ident), depuis=b.get("depuis"),
                 prix=b.get("prix"), reserve=b.get("reserve"), emplacement=choix or "",
                 absente=bool(choix) and par_id[choix].get("present") is False)
        if f["bas"] and f["reserve"] == 0:
            a_racheter.append(" ".join(x for x in (f["nom"], f["marque"], f["type"]) if x))
        cartes.append(f)
    for cle in sorted((c for c in sans_bobine if c in puces), key=cle_tri):  # bobine à puce, pas suivie ici
        f = fiche(cle, sans_bobine[cle])
        f.update(jauge(*puces[cle]), source="puce", id="", suivie=False, marque="Bambu Lab", depuis=None, prix=None,
                 reserve=None, emplacement="", absente=False)
        reste_total += max(f["reste"], 0)
        cartes.append(f)

    # Fils chargés sur l'imprimante ou imprimés depuis que BlokBambu compte, sans bobine suivie. Ceux
    # de l'historique repris sur la carte ne sont pas proposés : ce sont souvent des bobines finies depuis.
    avec_bobine = {cle_fil(b) for b in bobines.values()}
    a_suivre = []
    for cle in sorted((c for c in imprimees | set(sans_bobine)
                       if c not in puces and (c in sans_bobine or c not in avec_bobine)), key=cle_tri):
        f = fiche(cle, sans_bobine.get(cle, []))
        f["marque"] = marque_probable(cle)
        a_suivre.append(f)

    # Couleurs proposées à l'ajout : d'abord celles qui attendent d'être suivies.
    proposees = list(dict.fromkeys([f["cle"] for f in a_suivre] + sorted((c for c in connues if c not in avec_bobine), key=cle_tri)
                                   + sorted(avec_bobine, key=cle_tri)))

    en_cours = {v["tache"]["ident"]: v["tache"]["percent"] for v in vivantes.values() if v["tache"]}
    imprimantes = []
    for repere, _ in ecoutees:
        v = vivantes.get(repere) or {"etat": "recherche", "tache": None}
        tache, machine = None, machines.get(repere) or {"total": compte(), "mois": compte()}
        if v["tache"]:
            liee = next((en for en in journal if en.get("tache") == v["tache"]["ident"]
                         and en.get("etat", "lancee") == "lancee"), None)
            tache = {"projet": liee["projet"] if liee else nom_tache(v["tache"]["nom"]), "percent": v["tache"]["percent"],
                     "pause": v["tache"]["pause"]}
        charge = []
        for p in places:
            o = occupe.get(p["id"])
            if p["imprimante"] != repere or (p["bac"] == EXTERNE and not o):
                continue
            couleur = o["cle"].partition("|")[0] if o else ""
            charge.append({"nom": p["nom"], "couleur": couleur, "teinte": nom_couleur(couleur) if o else "",
                           "type": o["cle"].partition("|")[2] if o else "",
                           "marque": (bobines.get(o["bobine"]) or {}).get("marque", "") if o else ""})
        imprimantes.append({"id": repere, "nom": noms[repere], "etat": v["etat"], "court": ETATS_COURTS.get(v["etat"], ""),
                            "texte": ETATS_IMPRIMANTE.get(v["etat"], ""), "alerte": v["etat"] in ("bloquee", "refusee"),
                            "impression": tache, "repos": bool(v.get("entendue")) and v["etat"] == "connectee" and not tache,
                            "erreurs": v.get("erreurs") or [],
                            "emplacements": charge, "total": chiffres(machine["total"]),
                            "mois": chiffres(machine["mois"])})

    courant = mois.get(ce_mois, compte())
    total = compte()
    for m in mois.values():
        total["secondes"] += m["secondes"]
        if m["prix"]:
            total["cout"], total["prix"] = total["cout"] + m["cout"], True
    etat = {
        "imprimantes": imprimantes,
        "machines": [{"id": r, "nom": nom} for r, nom in noms.items()],
        "resume": {"impressions": len(journal), "depuis": journal[0]["date"] if journal else None,
                   "g": round(sum(t["g"] for t in totaux.values()), 2), "reste": round(reste_total, 2),
                   "cout": round(total["cout"], 2) if total["prix"] else None, "secondes": round(total["secondes"]),
                   "mois": dict(chiffres(courant), nom=MOIS[maintenant.month - 1])},
        "bobines": cartes,
        "a_suivre": a_suivre,
        "a_racheter": a_racheter,
        "couleurs": [fiche(c) for c in proposees],
        "emplacements": [{"id": p["id"], "nom": p["nom"], "imprimante": noms.get(p["imprimante"], "") if plusieurs else ""}
                         for p in places],
        "totaux": [dict(fiche(cle), fils=t["noms"], g=round(t["g"], 2), m=round(t["m"], 2), n=t["n"])
                   for cle, t in sorted(totaux.items(), key=lambda x: -x[1]["g"])],
        "mois": [dict(chiffres(m), cle=cle, nom="%s %s" % (MOIS[int(cle[5:7]) - 1], cle[:4]))
                 for cle, m in sorted(mois.items(), reverse=True)],
        "impressions": [{"id": identifiant(en), "date": en["date"], "projet": en["projet"],
                         "secondes": round(temps(en)), "part": part(en), "cout": cout(en),
                         "en_cours": en_cours.get(en.get("tache")) if en.get("etat", "lancee") == "lancee" else None,
                         "imprimante": imprimante_de(en, memoire) or "", "source": en.get("source", "studio"),
                         "a_completer": bool(en.get("a_completer")),
                         "fils": [{"couleur": f["couleur"], "nom": nom_couleur(f["couleur"]), "type": f["type"],
                                   "g": round(f["g"] * part(en), 2), "pese": f["g"],
                                   "bobine": f["bobine"] if f.get("bobine") in bobines else "",
                                   "relais": [{"a": round(r["a"] * 100, 1),
                                               "bobine": r["bobine"] if r.get("bobine") in bobines else ""}
                                              for r in f.get("relais") or []]}
                                  for f in en["fils"]]}
                        for en in reversed(journal[-300:])],
        "alertes": list(reversed(lire_alertes(donnees))),
        "marques": marques_proposees(donnees),
        "types": TYPES,
        "local": local,
        "site": SITE,
        "version": version_installee(),
        "maj": nouveautes.annonce() if nouveautes else None,
        "lecture_carte": reglages["carte"],
    }
    if local:  # les réglages ne se voient et ne se changent que depuis l'ordinateur lui-même
        adresse = adresse_locale()
        etat["reglages"] = {"reseau": reglages["reseau"], "code": reglages["code"], "seuil": reglages["seuil"],
                            "fin": reglages["fin"], "carte": reglages["carte"],
                            "adresse": "http://%s:%d/" % (adresse, port) if adresse else None,
                            "imprimantes": parc.reglages() if parc else None,
                            "historique": dict(parc.historique) if parc and parc.historique else None}
    return etat


def ecrire_page(donnees, port):
    ecrire(os.path.join(donnees, PAGE), RACCOURCI.replace("PORT", str(port)))


# ---------------------------------------------------------------- serveur de la page

def bobine_demandee(champs):
    """(couleur, type) d'un envoi de la page, ou None si la saisie n'est pas valable."""
    couleur, type_fil = champs.get("couleur", "").strip().upper(), champs.get("type", "")
    if not re.fullmatch(r"#[0-9A-F]{6}", couleur) or not re.fullmatch(r"[A-Za-z0-9+\- ]{1,16}", type_fil):
        return None
    return couleur, type_fil


def nombre_saisi(champs, nom, maxi, entier=False):
    """Un nombre facultatif d'un envoi : (True, valeur ou None) s'il est acceptable, (False, None) sinon."""
    texte = champs.get(nom, "").strip().replace(",", ".")
    if not texte:
        return True, None
    try:
        valeur = float(texte)
    except ValueError:
        return False, None
    if not 0 <= valeur <= maxi or (entier and valeur != int(valeur)):
        return False, None
    return True, int(valeur) if entier else round(valeur, 2)


def fils_saisis(texte, bobines):
    """Les fils saisis sur la page pour une impression : [{couleur, type, g, bobine}], ou None si la saisie ne tient pas.

    Chaque ligne désigne une bobine suivie, ou à défaut une couleur et un type de fil.
    """
    try:
        lignes = json.loads(texte)
    except ValueError:
        return None
    if not isinstance(lignes, list) or len(lignes) > 16:
        return None
    fils = []
    for ligne in lignes:
        if not isinstance(ligne, dict):
            return None
        try:
            grammes = round(float(str(ligne.get("g", 0)).replace(",", ".")), 2)
        except ValueError:
            return None
        if not 0 <= grammes <= 20000:
            return None
        ident = str(ligne.get("bobine") or "")
        if ident in bobines:
            fil = {"couleur": bobines[ident]["couleur"], "type": bobines[ident]["type"], "bobine": ident}
        else:
            choisie = bobine_demandee({"couleur": str(ligne.get("couleur", "")), "type": str(ligne.get("type", ""))})
            if not choisie:
                return None
            fil = {"couleur": choisie[0], "type": choisie[1]}
        # Bobine changée en cours d'impression : à partir de tel avancement, telle bobine (ou aucune suivie).
        relais = ligne.get("relais") or []
        if not isinstance(relais, list) or len(relais) > 5:
            return None
        for r in relais:
            try:
                a = round(float(str(r.get("a")).replace(",", ".")), 1)
            except (AttributeError, ValueError):
                return None
            suite = str(r.get("bobine") or "")
            if not 0 < a < 100 or (suite and suite not in bobines):
                return None
            fil.setdefault("relais", []).append({"a": round(a / 100, 4), "bobine": suite or None})
        if fil.get("relais"):
            fil["relais"].sort(key=lambda r: r["a"])
        fils.append(dict(fil, g=grammes))
    return fils


class Serveur(ThreadingHTTPServer):
    # Sous Windows, cette option laisserait deux exemplaires écouter sur le même port.
    allow_reuse_address = not WINDOWS
    daemon_threads = True

    def server_bind(self):
        # HTTPServer cherche ici le nom de l'ordinateur sur le réseau, ce qui peut bloquer des dizaines
        # de secondes : ce nom est inutile.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


def lancer_serveur(donnees, port, parc=None, reseau=False, nouveautes=None):
    """Sert la page. Avec `reseau`, les appareils du même réseau y accèdent aussi, après avoir donné le code."""
    essais = {}  # adresse → heures des codes faux, pour freiner qui en essaierait beaucoup

    class Page(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def local(self):
            return ipaddress.ip_address(self.client_address[0]).is_loopback

        def autorise(self):
            """Refuse ce qui ne vient pas de la page elle-même, demandée sous une adresse attendue."""
            hote = self.headers.get("Host", "")
            if self.local():
                bon = hote in ("127.0.0.1:%d" % port, "localhost:%d" % port)
            else:  # depuis le réseau : seulement par l'adresse locale de cet ordinateur
                nom, _, port_demande = hote.rpartition(":")
                try:
                    bon = port_demande == str(port) and ipaddress.ip_address(nom).is_private
                except ValueError:
                    bon = False
            if bon and self.command == "POST":
                return self.headers.get("Origin") == "http://" + hote
            return bon

        def connecte(self):
            """Sur l'ordinateur lui-même, rien à prouver ; ailleurs, il faut avoir donné le code."""
            if self.local():
                return True
            reglages = lire_reglages(donnees)
            if not reglages["reseau"] or not reglages["code"]:
                return False
            recus = re.findall(r"(?:^|;\s*)blokbambu=([0-9a-f]{64})", self.headers.get("Cookie", ""))
            return jeton_reseau(reglages) in recus

        def repondre(self, code, corps, type_mime="text/plain; charset=utf-8", cache="no-store", entetes=()):
            self.send_response(code)
            self.send_header("Content-Type", type_mime)
            self.send_header("Content-Length", str(len(corps)))
            self.send_header("Cache-Control", cache)
            for nom, valeur in entetes:
                self.send_header(nom, valeur)
            self.end_headers()
            self.wfile.write(corps)

        def json(self, code, valeur):
            self.repondre(code, json.dumps(valeur, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def bon(self, reussi=True, **plus):
            self.json(200 if reussi else 400, dict(plus, ok=bool(reussi)))

        def fichier(self, chemin, type_mime, cache="no-store"):
            try:
                with open(chemin, "rb") as fichier:
                    self.repondre(200, fichier.read(), type_mime, cache)
            except OSError:
                self.repondre(500, "Il manque un fichier de BlokBambu : relancez l'installation.".encode("utf-8"))

        def page_code(self, message=""):
            self.repondre(200, PAGE_CODE.replace("MESSAGE", message).encode("utf-8"), "text/html; charset=utf-8")

        def do_GET(self):
            chemin = self.path.split("?")[0]
            if not self.autorise():
                self.repondre(403, b"refuse")
            elif not self.connecte():
                if chemin == "/code":
                    self.page_code()
                else:
                    self.repondre(303, b"", entetes=[("Location", "/code")])
            elif chemin == "/api/etat":
                self.json(200, etat_page(donnees, parc, self.local(), port, nouveautes))
            elif chemin == "/saut.js":
                self.repondre(200, b'location.replace("http://127.0.0.1:%d/");' % port, "text/javascript")
            elif chemin == "/code":
                self.repondre(303, b"", entetes=[("Location", "/")])
            elif chemin in FICHIERS_PAGE:
                nom, type_mime, cache = FICHIERS_PAGE[chemin]
                self.fichier(os.path.join(ICI, nom), type_mime, cache)
            else:
                self.repondre(404, b"introuvable")

        def do_POST(self):
            taille = int(self.headers.get("Content-Length") or 0)
            if not self.autorise() or taille > 4096:
                return self.repondre(403, b"refuse")
            champs = {k: v[0] for k, v in parse_qs(self.rfile.read(taille).decode("utf-8", "replace")).items()}
            if self.path == "/code":
                return self.entrer(champs)
            if not self.connecte():
                return self.repondre(403, b"refuse")
            if self.path == "/reglages" or self.path.startswith("/imprimante"):
                if not self.local():  # réglages et imprimantes : depuis l'ordinateur lui-même seulement
                    return self.repondre(403, b"refuse")
                return self.regler(champs) if self.path == "/reglages" else self.imprimante(champs)
            if self.path == "/alerte/fermer":
                with verrou_alertes:
                    ecrire_json(donnees, "alertes.json", [a for a in lire_alertes(donnees)
                                                          if champs.get("id") not in (a.get("id"), "toutes")])
                return self.bon()
            if self.path == "/impression/ajouter":
                return self.ajouter_impression(champs)
            if self.path in ("/impression", "/impression/supprimer"):
                return self.corriger(champs)
            if self.path in ("/bobine", "/bobine/modifier", "/retirer"):
                return self.bobine(champs)
            self.bon(False)

        # ----- bobines

        def bobine(self, champs):
            """Ajoute une bobine, la remplace par une neuve, la modifie ou cesse de la suivre."""
            ident = champs.get("id", "")
            bon_prix, prix = nombre_saisi(champs, "prix", 1000)
            bonne_reserve, reserve = nombre_saisi(champs, "reserve", 99, entier=True)
            bon_poids, grammes = nombre_saisi(champs, "poids", 20000)
            marque = " ".join(champs.get("marque", "").split())[:40]
            emplacement = champs.get("emplacement", "")
            with verrou:
                bobines = lire_bobines(donnees)
                if self.path == "/retirer":
                    if bobines.pop(ident, None) is None:
                        return self.bon(False)
                elif not bon_prix or not bonne_reserve or not re.fullmatch(EMPLACEMENT, emplacement):
                    return self.bon(False)
                elif self.path == "/bobine/modifier":
                    if ident not in bobines:
                        return self.bon(False)
                    bobines[ident].update(marque=marque, prix=prix or None, reserve=reserve)
                    poser_bobine(bobines, ident, emplacement)
                    # Le prix corrigé vaut pour toutes les impressions faites avec cette bobine.
                    journal = lire_journal(donnees)
                    for entree in journal:
                        if any(fil.get("bobine") == ident for fil in entree["fils"]):
                            poser_prix(entree, bobines)
                    ecrire_json(donnees, "impressions.json", journal)
                else:
                    choisie = bobine_demandee(champs)
                    if not choisie or not bon_poids or not grammes:
                        return self.bon(False)
                    # Une bobine neuve part de son poids : seules les impressions suivantes s'en déduisent.
                    # Celle qu'elle remplace n'est plus suivie ; ses impressions gardent leur coût.
                    bobines.pop(champs.get("remplace", ""), None)
                    ident = secrets.token_hex(4)
                    bobines[ident] = {"couleur": choisie[0], "type": choisie[1], "marque": marque, "poids": grammes,
                                      "prix": prix or None, "reserve": reserve,
                                      "depuis": datetime.now().isoformat(timespec="seconds")}
                    poser_bobine(bobines, ident, emplacement)
                if marque and marque not in marques_proposees(donnees):  # une marque nouvelle est retenue
                    ecrire_json(donnees, "marques.json", lire(donnees, "marques.json", []) + [marque])
                ecrire_json(donnees, "bobines.json", bobines)
            self.bon(id=ident)

        # ----- impressions

        def corriger(self, champs):
            """Corrige une impression (part faite, imprimante, bobines, grammes à compléter) ou la retire du compte."""
            bonne_part, pourcent = nombre_saisi(champs, "part", 100)
            machine = champs.get("imprimante", "")
            with verrou:
                journal, bobines = lire_journal(donnees), lire_bobines(donnees)
                entree = next((e for e in comptees(journal) if identifiant(e) == champs.get("id")), None)
                if not entree:
                    return self.bon(False)
                if self.path == "/impression/supprimer":
                    entree["supprimee"] = True  # gardée, pour ne pas être recomptée si son fichier est relu
                else:
                    fils = fils_saisis(champs["fils"], bobines) if "fils" in champs else []
                    if not bonne_part or pourcent is None or fils is None or (machine and machine not in imprimantes_gardees(donnees)):
                        return self.bon(False)
                    if round(pourcent / 100, 4) != round(part(entree), 4):
                        entree["part"], entree["corrigee"] = round(pourcent / 100, 4), True
                    if machine and machine != imprimante_de(entree, imprimantes_gardees(donnees)):
                        entree["imprimante"], entree["imprimante_choisie"] = machine, True
                    if "fils" in champs and not self.poser_fils(entree, fils, bobines):
                        return self.bon(False)
                    poser_prix(entree, bobines)
                ecrire_json(donnees, "impressions.json", journal)
            self.bon()

        def poser_fils(self, entree, fils, bobines):
            """Les fils saisis sur la page. Une impression lue dans un fichier garde ses grammes : seule
            la bobine de chaque fil se choisit. Les autres (ajoutée à la main, lancée ailleurs) se saisissent en entier."""
            if entree.get("source") in ("main", "imprimante"):
                peses = [fil for fil in fils if fil["g"] > 0]
                if not peses:
                    return entree.get("source") == "imprimante" and bool(entree.get("a_completer"))
                entree["fils"] = [dict(fil, nom="", m=metres(fil["g"], fil["type"]), bobine_choisie=True,
                                       relais_choisis=True) for fil in peses]
                entree.pop("a_completer", None)
                return True
            if len(fils) != len(entree["fils"]):
                return False
            for ancien, saisi in zip(entree["fils"], fils):
                # Une bobine remplacée depuis reste notée sur le fil, avec son prix, tant qu'on n'y touche pas.
                if saisi.get("bobine") != (ancien.get("bobine") if ancien.get("bobine") in bobines else None):
                    if saisi.get("bobine"):
                        if cle_fil(saisi) != cle_fil(ancien):
                            ancien.setdefault("projet", {"couleur": ancien["couleur"], "type": ancien["type"]})
                        ancien.update(couleur=saisi["couleur"], type=saisi["type"], bobine=saisi["bobine"])
                    else:
                        ancien.pop("bobine", None)
                    ancien["bobine_choisie"] = True
                avant = [(r["a"], r.get("bobine") if r.get("bobine") in bobines else None) for r in ancien.get("relais") or []]
                if [(r["a"], r["bobine"]) for r in saisi.get("relais", [])] != avant:
                    if saisi.get("relais"):
                        ancien["relais"] = saisi["relais"]
                    else:
                        ancien.pop("relais", None)
                    ancien["relais_choisis"] = True
            return True

        def ajouter_impression(self, champs):
            """Une impression notée à la main : celle qu'aucun fichier de Bambu Studio n'a annoncée."""
            projet = " ".join(champs.get("projet", "").split())[:80]
            bonne_duree, minutes = nombre_saisi(champs, "minutes", 60 * 24 * 30, entier=True)
            machine = champs.get("imprimante", "")
            try:
                date = datetime.strptime(champs.get("date", "")[:16], "%Y-%m-%dT%H:%M")
            except ValueError:
                date = None
            with verrou:
                bobines, memoire = lire_bobines(donnees), imprimantes_gardees(donnees)
                fils = [fil for fil in fils_saisis(champs.get("fils", ""), bobines) or [] if fil["g"] > 0]
                if (not projet or not date or date > datetime.now() + timedelta(days=1) or not bonne_duree or not fils
                        or (machine and machine not in memoire)):
                    return self.bon(False)
                entree = {"cle": "main|" + secrets.token_hex(8), "date": date.isoformat(timespec="seconds"),
                          "projet": projet, "fichier": "", "etat": "terminee", "source": "main",
                          "secondes": (minutes or 0) * 60,
                          "fils": [dict(fil, nom="", m=metres(fil["g"], fil["type"]), bobine_choisie=True,
                                        relais_choisis=True) for fil in fils]}
                seules = [r for r, _ in suivies(memoire)] if parc else []
                if machine:
                    entree["imprimante"], entree["imprimante_choisie"] = machine, True
                elif len(seules) == 1:
                    entree["imprimante"] = seules[0]
                poser_prix(entree, bobines)
                journal = lire_journal(donnees) + [entree]
                journal.sort(key=lambda e: e["date"])
                ecrire_json(donnees, "impressions.json", journal)
            self.bon(id=identifiant(entree))

        # ----- réglages et imprimantes, depuis l'ordinateur lui-même

        def regler(self, champs):
            bon_seuil, seuil = nombre_saisi(champs, "seuil", 5000, entier=True)
            if not bon_seuil:
                return self.bon(False)
            with verrou:
                reglages = lire_reglages(donnees)
                if "reseau" in champs:
                    reglages["reseau"] = champs["reseau"] == "1"
                    if reglages["reseau"] and not reglages["code"]:
                        reglages["code"] = "%06d" % secrets.randbelow(10 ** 6)
                        reglages["secret"] = secrets.token_hex(16)
                if seuil is not None:
                    reglages["seuil"] = seuil
                for nom in ("fin", "carte"):
                    if nom in champs:
                        reglages[nom] = champs[nom] == "1"
                ecrire_json(donnees, "reglages.json", reglages)
            self.bon()

        def imprimante(self, champs):
            """Nommer une imprimante, cesser de la suivre, la reprendre, en ajouter une à la main."""
            if not parc:
                return self.bon(False)
            repere, nom = champs.get("id", ""), " ".join(champs.get("nom", "").split())[:24]
            if self.path == "/imprimante":
                return self.bon(parc.renommer(repere, nom))
            if self.path == "/imprimante/retirer":
                return self.bon(parc.retirer(repere))
            if self.path == "/imprimante/suivre":
                return self.bon(parc.reprendre(repere))
            if self.path == "/imprimante/historique":
                return self.bon(parc.reprendre_historique())
            if self.path != "/imprimante/ajouter":
                return self.bon(False)
            adresse, code = adresse_saisie(champs.get("adresse", "")), champs.get("code", "").strip()
            if not adresse:
                return self.bon(False, erreur="Cette adresse n'est pas celle d'un appareil du réseau local. "
                                              "Elle ressemble à 192.168.1.42.")
            if not re.fullmatch(r"[A-Za-z0-9]{4,32}", code):
                return self.bon(False, erreur="Le code d'accès se compose de chiffres et de lettres, sans espace.")
            try:
                parc.ajouter(adresse, code, nom)
            except Refus:
                erreur = "L'imprimante refuse ce code d'accès. Vérifiez-le sur son écran."
            except (AutreAppareil, ssl.SSLError):
                erreur = "L'appareil à cette adresse n'est pas reconnu comme une imprimante Bambu Lab."
            except (OSError, ValueError, struct.error) as e:
                erreur = (ETATS_IMPRIMANTE["bloquee"] if MAC and getattr(e, "errno", None) == 65 else
                          "Imprimante injoignable à cette adresse. Est-elle allumée, et sur le même réseau que cet ordinateur ?")
            else:
                return self.bon()
            self.bon(False, erreur=erreur)

        def entrer(self, champs):
            """Un appareil du réseau donne le code : cinq essais faux par quart d'heure, pas plus."""
            adresse, maintenant = self.client_address[0], time.time()
            faux = [t for t in essais.get(adresse, []) if maintenant - t < 900]
            reglages = lire_reglages(donnees)
            if self.local() or not reglages["reseau"] or not reglages["code"]:
                return self.repondre(303, b"", entetes=[("Location", "/")])
            if len(faux) >= 5:
                return self.page_code("Trop d'essais. Réessayez dans un quart d'heure.")
            if not secrets.compare_digest(champs.get("code", "").strip(), reglages["code"]):
                essais[adresse] = faux + [maintenant]
                return self.page_code("Ce n'est pas le bon code.")
            self.repondre(303, b"", entetes=[("Location", "/"), (
                "Set-Cookie", "blokbambu=%s; Max-Age=31536000; Path=/; HttpOnly; SameSite=Strict" % jeton_reseau(reglages))])

    serveur = Serveur(("0.0.0.0" if reseau else "127.0.0.1", port), Page)
    threading.Thread(target=serveur.serve_forever, daemon=True).start()
    return serveur


# ---------------------------------------------------------------- imprimantes

class Refus(Exception):
    pass


class AutreAppareil(Exception):
    pass


def acces_imprimantes(conf):
    """{numéro de série: code d'accès} des imprimantes connues de Bambu Studio. Les codes ne sont écrits nulle part."""
    try:
        with open(conf, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    acces = {}
    for liste in ("user_access_code", "access_code"):
        for serie, code in (d.get(liste) or {}).items():
            if code and serie not in acces:
                acces[serie] = code
    return acces


def adresses_vues():
    """Adresses locales vers lesquelles cet ordinateur a une liaison ouverte sur le port des imprimantes Bambu.

    C'est la liaison que Bambu Studio garde vers l'imprimante : on apprend ainsi son adresse sans rien
    chercher sur le réseau.
    """
    if MAC:
        commandes = [["/usr/sbin/lsof", "-nP", "-a", "-c", "BambuStu", "-iTCP:8883"]]
    elif WINDOWS:
        commandes = [["netstat", "-n", "-p", "TCP"]]
    else:
        commandes = [["ss", "-tn"], ["netstat", "-tn"]]
    for commande in commandes:
        try:
            sortie = subprocess.run(commande, capture_output=True, text=True, timeout=10, **SANS_FENETRE).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        return adresses_dans(sortie)
    return []


def adresses_dans(sortie):
    trouvees = []
    for m in re.finditer(r"(\d{1,3}(?:\.\d{1,3}){3}):8883\b", sortie):
        try:
            ip = ipaddress.ip_address(m.group(1))
        except ValueError:
            continue
        if ip.is_private and not ip.is_loopback and not ip.is_unspecified and m.group(1) not in trouvees:
            trouvees.append(m.group(1))
    return trouvees


def deja_lance(port):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/saut.js" % port, timeout=2) as r:
            return b"location.replace" in r.read()
    except Exception:
        return False


def nom_emplacement(cle):
    if cle == EXTERNE:
        return "Ext."
    unite, _, bac = cle.partition("-")
    return "%s%d" % (chr(65 + int(unite)), int(bac) + 1)  # A1 à A4, comme dans Bambu Studio


def chaine_mqtt(texte):
    octets = texte.encode("utf-8")
    return struct.pack("!H", len(octets)) + octets


def paquet_mqtt(entete, corps):
    taille, longueur = len(corps), b""
    while True:
        taille, reste = divmod(taille, 128)
        longueur += bytes([reste | (128 if taille else 0)])
        if not taille:
            return bytes([entete]) + longueur + corps


class Flux:
    """Découpe ce que l'imprimante envoie en paquets MQTT."""

    def __init__(self, liaison):
        self.liaison, self.tampon = liaison, bytearray()

    def paquet(self):
        """Rend (en-tête, corps), ou None si rien de complet n'est arrivé avant le délai."""
        while True:
            complet = self.extraire()
            if complet:
                return complet
            try:
                bloc = self.liaison.recv(65536)
            except (socket.timeout, ssl.SSLWantReadError):
                return None
            if not bloc:
                raise ConnectionError("liaison fermée par l'imprimante")
            self.tampon += bloc

    def extraire(self):
        t = self.tampon
        taille, decalage = 0, 0
        for i in range(1, 5):
            if len(t) <= i:
                return None
            taille |= (t[i] & 127) << decalage
            decalage += 7
            if not t[i] & 128:
                debut = i + 1
                if len(t) < debut + taille:
                    return None
                entete, corps = t[0], bytes(t[debut:debut + taille])
                del t[:debut + taille]
                return entete, corps
        raise ConnectionError("paquet illisible")


def nom_tache(nom):
    """Le nom d'une impression tel que l'imprimante l'annonce, sans dossier, extension ni numéro de plateau."""
    nom = str(nom or "").replace("\\", "/").rsplit("/", 1)[-1]
    nom = re.sub(r"\.(gcode\.3mf|3mf|gcode)$", "", nom, flags=re.I)
    return re.sub(r"_plate_\d+$", "", nom) or "Impression"


def lire_acces(donnees):
    """{repère: {adresse, code}} des imprimantes ajoutées à la main. C'est le seul fichier de BlokBambu
    où un code d'accès est écrit ; il ne quitte pas cet ordinateur et la page ne le montre jamais."""
    a = lire(donnees, "acces.json", {})
    if not isinstance(a, dict):
        return {}
    return {r: fiche for r, fiche in a.items() if isinstance(fiche, dict) and fiche.get("code") and fiche.get("adresse")}


def ecrire_acces(donnees, acces):
    chemin = os.path.join(donnees, "acces.json")
    provisoire = chemin + ".tmp"
    try:
        os.remove(provisoire)
    except OSError:
        pass
    # Lisible par la seule personne de cette session, dès sa création.
    with os.fdopen(os.open(provisoire, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as fichier:
        json.dump(acces, fichier, indent=1)
    os.replace(provisoire, chemin)


def adresse_saisie(texte):
    """(hôte, port) d'une adresse d'imprimante saisie à la main : une adresse du réseau local, rien d'autre."""
    hote, _, port = str(texte or "").strip().partition(":")
    try:
        ip = ipaddress.ip_address(hote)
        port = int(port) if port else 8883
    except ValueError:
        return None
    if ip.version != 4 or not ip.is_private or ip.is_unspecified or not 0 < port < 65536:
        return None
    return str(ip), port


def contexte_bambu(ac=None):
    """Ne fait confiance qu'aux certificats de Bambu Lab : ceux livrés ici, et celui de Bambu Studio s'il est installé."""
    contexte = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    contexte.check_hostname = False  # le certificat porte le numéro de série, pas l'adresse
    if ac:
        contexte.load_verify_locations(ac)
    else:
        contexte.load_verify_locations(cadata=AC_BAMBU)
        for certificat in certificats_installes():
            try:
                contexte.load_verify_locations(certificat)
            except (OSError, ssl.SSLError):
                pass
    return contexte


def serie_du_certificat(liaison):
    return dict(x[0] for x in liaison.getpeercert().get("subject", ())).get("commonName") or ""


def ouvrir_liaison(hote, port, code, client, ac=None, attendue=None, delai=25):
    """Joint une imprimante et lui présente le code d'accès. Rend (liaison, flux, numéro de série).

    Le certificat doit venir de Bambu Lab. `attendue` dit quelle imprimante on veut (numéro de série
    ou repère) : à une autre, le code n'est pas présenté.
    """
    # Une imprimante occupée met parfois plus de dix secondes à établir la liaison chiffrée.
    liaison = contexte_bambu(ac).wrap_socket(socket.create_connection((hote, port), timeout=delai))
    try:
        serie = serie_du_certificat(liaison)
        if not serie or (attendue and attendue not in (serie, repere_de(serie))):
            raise AutreAppareil()
        liaison.sendall(paquet_mqtt(0x10, chaine_mqtt("MQTT") + bytes([4, 0xC2]) + struct.pack("!H", 60)
                                    + chaine_mqtt(client) + chaine_mqtt("bblp") + chaine_mqtt(code)))
        flux = Flux(liaison)
        reponse = flux.paquet()
        if not reponse or reponse[0] != 0x20 or len(reponse[1]) < 2:
            raise ConnectionError("pas de réponse de l'imprimante")
        if reponse[1][1] != 0:
            raise Refus("code d'accès refusé")
        return liaison, flux, serie
    except BaseException:
        liaison.close()
        raise


class Carte(ftplib.FTP_TLS):
    """La carte mémoire d'une imprimante, pour y lire : FTP chiffré dès la connexion, comme Bambu Studio.

    Aucune commande d'écriture n'est écrite ici : ni envoi, ni suppression, ni renommage.
    """

    def connect(self, host, port, timeout):
        self.host, self.port, self.timeout = host, port, timeout
        brut = socket.create_connection((host, port), timeout)
        self.af = brut.family
        self.sock = self.context.wrap_socket(brut)
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome

    def ntransfercmd(self, cmd, rest=None):
        conn, taille = ftplib.FTP.ntransfercmd(self, cmd, rest)
        return self.context.wrap_socket(conn, session=self.sock.session), taille  # même session chiffrée que les commandes

    def taille(self, chemin):
        """Taille d'un fichier, ou None s'il n'existe pas."""
        try:
            return int(self.sendcmd("SIZE " + chemin).split()[-1])
        except (ftplib.error_perm, ValueError, IndexError):
            return None

    def lire(self, chemin):
        self.voidcmd("TYPE I")
        return self.recevoir("RETR " + chemin)

    def lister(self, dossier):
        """Les fichiers d'un dossier : [(nom, taille, date)], la date à la minute, telle que l'imprimante l'affiche."""
        maintenant, fichiers = datetime.now(), []
        for ligne in self.recevoir("LIST " + dossier).decode("utf-8", "replace").splitlines():
            m = LIGNE_LISTE.match(ligne.strip())
            if not m or m.group(2).lower() not in MOIS_ANGLAIS:
                continue
            taille, mois, jour, heure, minute, annee, nom = m.groups()
            try:
                date = datetime(int(annee or maintenant.year), MOIS_ANGLAIS.index(mois.lower()) + 1, int(jour),
                                int(heure or 0), int(minute or 0))
            except ValueError:
                continue
            if not annee and date > maintenant + timedelta(days=1):  # l'année n'est pas dite pour les fichiers récents
                date = date.replace(year=date.year - 1)
            fichiers.append((nom, int(taille), date))
        return fichiers

    def recevoir(self, commande):
        conn, _ = self.ntransfercmd(commande)
        recu = bytearray()
        try:
            while True:
                bloc = conn.recv(65536)
                if not bloc:
                    break
                recu += bloc
            try:  # fin propre du canal chiffré : sans elle, l'imprimante garde son service de fichiers occupé plusieurs minutes
                conn.unwrap()
            except (OSError, ValueError):
                pass
        finally:
            conn.close()
        self.voidresp()
        return bytes(recu)


MOIS_ANGLAIS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
LIGNE_LISTE = re.compile(r"^-\S+\s+\d+\s+\S+\s+\S+\s+(\d+)\s+([A-Za-z]{3})\s+(\d{1,2})\s+(?:(\d{1,2}):(\d{2})|(\d{4}))\s+(.+)$")
MAXI_CARTE = 40 * 2 ** 20  # au-delà, un fichier d'impression n'est pas lu : trop long à transférer


def ouvrir_carte(hote, port, code, serie, ac=None):
    """Ouvre en lecture la carte mémoire de l'imprimante attendue. À refermer par `fermer_carte`."""
    carte = Carte(context=contexte_bambu(ac))
    carte.connect(hote, port, 25)
    try:
        if serie_du_certificat(carte.sock) != serie:  # le code n'est présenté qu'à l'imprimante attendue
            raise AutreAppareil()
        carte.login("bblp", code)
        carte.prot_p()
        return carte
    except BaseException:
        carte.close()
        raise


def fermer_carte(carte):
    try:
        carte.quit()
    except Exception:
        carte.close()


def envoi_du_3mf(donnees, plateau=None):
    """Ce que dit un fichier d'impression lu sur la carte, ou None s'il ne se lit pas."""
    try:
        with zipfile.ZipFile(io.BytesIO(donnees)) as z:
            noms = set(z.namelist())
            if "Metadata/slice_info.config" not in noms:
                return None
            return envoi_de(ET.fromstring(z.read("Metadata/slice_info.config")), noms, [], plateau)
    except (zipfile.BadZipFile, ET.ParseError, KeyError, ValueError):
        return None


def note_carte(carte, dossier, nom, plateau):
    """La note que l'imprimante garde de chaque impression : de quel emplacement est parti chaque fil
    du projet. {numéro du fil: emplacement}, vide si elle n'y est pas."""
    chemin = "%s/%d_%s.bbl" % (dossier, plateau, nom)
    if not nom or "\r" in chemin or "\n" in chemin or (carte.taille(chemin) or 65536) >= 65536:
        return {}
    try:
        emplacements = json.loads(carte.lire(chemin).decode("utf-8", "replace")).get("ams mapping") or []
    except (ValueError, AttributeError):
        return {}
    bacs = {}
    for rang, bac in enumerate(emplacements if isinstance(emplacements, list) else []):
        if isinstance(bac, int) and 0 <= bac < 16:
            bacs[rang + 1] = "%d-%d" % (bac // 4, bac % 4)
        elif bac == 254:
            bacs[rang + 1] = EXTERNE
    return bacs


def lire_note(hote, port, code, serie, nom, plateau, ac=None):
    carte = ouvrir_carte(hote, port, code, serie, ac)
    try:
        return note_carte(carte, "/cache", nom, plateau)
    finally:
        fermer_carte(carte)


def chemins_carte(nom, fichier):
    """Où peut se trouver, sur la carte, le fichier d'une impression d'après ce que l'imprimante en annonce."""
    chemins = []
    if nom:
        chemins += ["/cache/%s.3mf" % nom, "/cache/%s.gcode.3mf" % nom, "/%s.3mf" % nom, "/%s.gcode.3mf" % nom]
    if fichier.lower().endswith(".3mf"):
        relatif = re.sub(r"^/?(mnt/)?sdcard/", "", fichier.replace("\\", "/")).lstrip("/")
        chemins += ["/" + relatif, "/cache/" + relatif.rsplit("/", 1)[-1]]
    return [c for c in dict.fromkeys(chemins) if "\r" not in c and "\n" not in c and ".." not in c]


def lire_carte(hote, port, code, serie, nom, fichier, ac=None, maxi=MAXI_CARTE):
    """Lit sur la carte de l'imprimante le fichier de l'impression qu'elle annonce sous ce nom.

    Rend ce qu'il dit — grammes par fil, durée prévue — et `bacs` : {numéro du fil: emplacement},
    quand l'imprimante l'a noté. None si le fichier n'y est pas ou pèse trop lourd.
    """
    carte = ouvrir_carte(hote, port, code, serie, ac)
    try:
        for chemin in chemins_carte(nom, fichier):
            taille = carte.taille(chemin)
            if taille is None:
                continue
            if taille > maxi:
                return None
            plateau = re.search(r"plate_(\d+)\.gcode$", fichier)
            envoi = envoi_du_3mf(carte.lire(chemin), plateau.group(1) if plateau else None)
            if envoi:
                envoi["bacs"] = note_carte(carte, chemin.rsplit("/", 1)[0], nom, envoi["plateaux"][0])
            return envoi
        return None
    finally:
        fermer_carte(carte)


def placer_fils(entree, bacs, occupe, repere, bobines):
    """Chaque fil du projet va à la bobine de l'emplacement que l'imprimante a noté pour lui : celle qui
    s'y trouvait au départ de l'impression, sinon celle qui s'y trouve. Rend True si un fil a changé."""
    premiers = (entree.get("suivi") or {}).get("premiers") or {}
    seul, change = len(entree["fils"]) == 1, False
    for f in entree["fils"]:
        bac = bacs.get(f.get("numero") or (1 if seul else None))
        if not bac or f.get("bobine_choisie"):
            continue
        avant = (f["couleur"], f["type"], f.get("bobine"), f.get("bac"))
        f["bac"] = bac
        o = premiers.get(bac) or occupe.get("%s:%s" % (repere, bac))
        if o and o.get("cle"):
            couleur, _, type_fil = o["cle"].partition("|")
            if (couleur, type_fil) != (f["couleur"], f["type"]):
                f.setdefault("projet", {"couleur": f["couleur"], "type": f["type"]})
            f.update(couleur=couleur, type=type_fil)
            if o.get("bobine"):
                f["bobine"] = o["bobine"]
            else:
                f.pop("bobine", None)
        change = change or avant != (f["couleur"], f["type"], f.get("bobine"), f["bac"])
    if change:
        poser_prix(entree, bobines)
    return appliquer_changements(entree, bobines) or change


def meme_impression(e, pesee, debut, fin):
    """Une impression du journal qui a le même poids par fil, dans cette fenêtre de temps."""
    return (not e.get("supprimee") and debut <= e["date"] <= fin
            and sorted(((f.get("projet") or f)["couleur"], (f.get("projet") or f)["type"], f["g"]) for f in e["fils"]) == pesee)


class Parc:
    """Les imprimantes suivies — celles que Bambu Studio connaît, celles ajoutées à la main —, une écoute chacune."""

    def __init__(self, donnees, fenetres, conf=None, ac=None, adresses=None, delais=(STABILISATION, REMPLACEMENT)):
        self.donnees, self.fenetres, self.conf, self.ac = donnees, fenetres, conf, ac
        self.adresses = adresses or {}  # essais : {numéro de série ou "*": (hôte, port)}
        self.stabilisation, self.remplacement = delais
        self.attente_studio = min(ATTENTE_STUDIO, 6 * self.stabilisation)
        self.port_carte = 990  # la carte mémoire des imprimantes se lit par là
        # La reprise de l'historique des cartes et où elle en est ; demandée, elle attend une imprimante au repos,
        # même si BlokBambu redémarre entre-temps.
        self.historique, self.fil_historique = lire(donnees, "historique.json", None), None
        if isinstance(self.historique, dict) and self.historique.get("etat") == "en cours":
            self.historique["etat"] = "en attente"  # interrompue par un arrêt : elle reprendra (sans doublons)
        self.cache_bobines = (None, {})
        self.imprimantes = {}  # repère → Imprimante écoutée
        self.connues = {}      # repère → numéro de série, pour celles que Bambu Studio connaît
        self.memoire = imprimantes_gardees(donnees)
        self.reprise = lire(donnees, "imprimante.json", None)  # mémoire d'avant, quand il n'y en avait qu'une
        self.ecriture = threading.RLock()  # la mémoire est lue et écrite par les écoutes et par la page

    def code(self, imprimante):
        """Le code d'accès : celui saisi ici pour une imprimante ajoutée à la main, sinon celui de Bambu Studio."""
        saisi = lire_acces(self.donnees).get(imprimante.repere)
        if saisi:
            return saisi["code"]
        return acces_imprimantes(self.conf or conf_bambu()).get(imprimante.serie) if imprimante.serie else None

    def actualiser(self):
        """Écoute les imprimantes à suivre qu'on n'écoutait pas encore, cesse d'écouter celles qui ne le sont plus."""
        with self.ecriture:
            self.connues = {repere_de(s): s for s in acces_imprimantes(self.conf or conf_bambu())}
            voulues = dict.fromkeys(list(self.connues) + list(lire_acces(self.donnees)))
            change = False
            for repere in voulues:
                fiche = self.memoire.get(repere)
                if fiche is None or "numero" not in fiche:
                    numero = max([m.get("numero", 0) for m in self.memoire.values()] + [0]) + 1
                    ancienne = self.reprise if numero == 1 and isinstance(self.reprise, dict) else {}
                    fiche = self.memoire[repere] = dict(ancienne, **dict(fiche or {}, numero=numero))
                    change = True
                if fiche.get("retiree") or repere in self.imprimantes:
                    continue
                self.imprimantes[repere] = Imprimante(self, fiche["numero"], repere, self.connues.get(repere))
                self.imprimantes[repere].start()
                change = True
            for repere in [r for r in self.imprimantes if r not in voulues or self.memoire[r].get("retiree")]:
                self.imprimantes.pop(repere).arret.set()
                change = True
            if change:
                self.garder()

    def garder(self):
        """Écrit la mémoire : ce que sait chaque imprimante écoutée, et lesquelles sont suivies."""
        with self.ecriture:
            for repere, fiche in self.memoire.items():
                imprimante = self.imprimantes.get(repere)
                fiche["suivie"] = imprimante is not None
                if imprimante:
                    fiche.update(ip=imprimante.ip, emplacements=imprimante.emplacements)
            ecrire_json(self.donnees, "imprimantes.json", self.memoire)

    def nom(self, repere):
        with self.ecriture:
            return nom_imprimante(self.memoire, repere)

    def bobines(self):
        """Les bobines suivies, relues seulement quand leur fichier a changé : on les consulte à chaque annonce."""
        try:
            repere = os.stat(os.path.join(self.donnees, "bobines.json")).st_mtime_ns
        except OSError:
            repere = None
        if repere != self.cache_bobines[0] or repere is None:
            self.cache_bobines = (repere, lire_bobines(self.donnees))
        return self.cache_bobines[1]

    def places(self):
        """Les emplacements de toutes les imprimantes suivies, tels qu'elles les annoncent en ce moment."""
        with self.ecriture:
            return emplacements_connus(suivies(self.memoire))

    def etats(self):
        """Ce que fait chaque imprimante écoutée : {repère: {etat, tache}}."""
        etats = {}
        for repere, imprimante in list(self.imprimantes.items()):
            t = imprimante.tache
            textes = textes_erreurs(imprimante.serie, self.conf)
            erreurs = sorted(set().union(*imprimante.erreurs.values()))
            etats[repere] = {"etat": imprimante.etat, "entendue": "gcode_state" in imprimante.vue,
                             "erreurs": [{"code": code_lisible(c), "texte": textes.get(c, "")} for c in erreurs],
                             "tache": t and {
                "ident": t["ident"], "percent": t["percent"], "pause": t["etat"] == "PAUSE",
                "nom": str(imprimante.vue.get("subtask_name") or imprimante.vue.get("gcode_file") or "")}}
        return etats

    def reglages(self):
        """Ce que les réglages de la page montrent des imprimantes. Jamais un code d'accès."""
        with self.ecriture:
            manuelles, liste = lire_acces(self.donnees), []
            for repere, fiche in sorted(self.memoire.items(), key=lambda x: x[1].get("numero", 0)):
                imprimante = self.imprimantes.get(repere)
                if not imprimante and not (fiche.get("retiree") and repere in self.connues):
                    continue  # ni suivie, ni possible à reprendre d'un clic
                sans_nom = dict(self.memoire, **{repere: {k: v for k, v in fiche.items() if k != "nom"}})
                liste.append({"id": repere, "nom": fiche.get("nom") or "", "defaut": nom_imprimante(sans_nom, repere),
                              "suivie": imprimante is not None,
                              "etat": ETATS_COURTS.get(imprimante.etat, "") if imprimante else "",
                              "adresse": manuelles[repere]["adresse"].replace(":8883", "") if repere in manuelles else ""})
            return liste

    def reprendre_historique(self):
        """Demande la reprise des impressions passées gardées sur la carte des imprimantes suivies. Elle
        part dès qu'une imprimante est allumée et au repos."""
        if self.fil_historique and self.fil_historique.is_alive():
            return False
        self.historique = {"etat": "en attente", "lus": 0, "total": 0, "ajoutees": 0, "deja": 0, "sautees": []}
        ecrire_json(self.donnees, "historique.json", self.historique)
        self.historique_si_possible()
        return True

    def historique_si_possible(self):
        """Lance la reprise demandée, si une imprimante est maintenant disponible."""
        h = self.historique
        if not isinstance(h, dict) or h.get("etat") != "en attente" or (self.fil_historique and self.fil_historique.is_alive()):
            return
        if not any(i.disponible() for i in list(self.imprimantes.values())):
            return
        h.update(etat="en cours", lus=0, total=0, ajoutees=0, deja=0, sautees=[])
        self.fil_historique = threading.Thread(target=self.lire_historique, daemon=True)
        self.fil_historique.start()

    def lire_historique(self):
        bilan = self.historique
        ecrire_json(self.donnees, "historique.json", bilan)
        for imprimante in list(self.imprimantes.values()):
            try:
                imprimante.reprendre_historique(bilan)
            except Exception as e:  # imprimante occupée, carte absente… : on le dit, sans s'arrêter là
                bilan["sautees"].append("%s : carte illisible" % self.nom(imprimante.repere))
                print("historique illisible :", type(e).__name__, flush=True)
        # Interrompue par une impression : elle reprendra après, sans relire ce qui est déjà ajouté.
        bilan["etat"] = "en attente" if bilan.pop("interrompue", False) else "fini"
        ecrire_json(self.donnees, "historique.json", bilan)
        print("historique repris :", bilan["ajoutees"], "impressions ajoutées", flush=True)

    def renommer(self, repere, nom):
        with self.ecriture:
            if repere not in self.memoire:
                return False
            if nom:
                self.memoire[repere]["nom"] = nom
            else:
                self.memoire[repere].pop("nom", None)
            self.garder()
            return True

    def retirer(self, repere):
        """Cesse de suivre une imprimante. Le code saisi ici pour elle est effacé ; ses impressions restent."""
        with self.ecriture:
            if repere not in self.memoire:
                return False
            self.memoire[repere]["retiree"] = True
            acces = lire_acces(self.donnees)
            if acces.pop(repere, None):
                ecrire_acces(self.donnees, acces)
            self.actualiser()
            self.garder()
            return True

    def reprendre(self, repere):
        with self.ecriture:
            if repere not in self.memoire:
                return False
            self.memoire[repere].pop("retiree", None)
            self.actualiser()
            self.garder()
            return True

    def ajouter(self, adresse, code, nom):
        """Ajoute une imprimante à la main, après l'avoir jointe : son certificat dit laquelle c'est."""
        hote, port = adresse
        liaison, _, serie = ouvrir_liaison(hote, port, code, "blokbambu-%d-ajout" % os.getpid(), self.ac, delai=20)
        liaison.close()
        repere = repere_de(serie)
        with self.ecriture:
            acces = lire_acces(self.donnees)
            acces[repere] = {"adresse": "%s:%d" % (hote, port), "code": code}
            ecrire_acces(self.donnees, acces)
            fiche = self.memoire.setdefault(repere, {})
            fiche.pop("retiree", None)
            if nom:
                fiche["nom"] = nom
            deja = self.imprimantes.pop(repere, None)  # déjà écoutée avec le code de Bambu Studio : elle repart avec celui-ci
            if deja:
                deja.arret.set()
            self.actualiser()
            self.garder()
        print("imprimante ajoutée à la main :", self.nom(repere), flush=True)
        return repere

    def adresse_trouvee(self, repere, hote, port):
        """Une imprimante ajoutée à la main a changé d'adresse sur le réseau : la nouvelle est retenue."""
        with self.ecriture:
            acces = lire_acces(self.donnees)
            if repere in acces and acces[repere]["adresse"] != "%s:%d" % (hote, port):
                acces[repere]["adresse"] = "%s:%d" % (hote, port)
                ecrire_acces(self.donnees, acces)


class Imprimante(threading.Thread):
    """Écoute l'état d'une imprimante sur le réseau local.

    Elle s'abonne à ses annonces et ne lui publie rien : aucune commande ne part. Elle en tire les
    bobines chargées ou retirées, l'impression en cours, son avancement et sa fin.
    """

    EN_COURS = ("PREPARE", "RUNNING", "PAUSE", "SLICING")

    def __init__(self, parc, numero, repere, serie=None):
        super().__init__(daemon=True)
        # Le numéro de série n'est gardé qu'en mémoire vive : Bambu Studio le donne, ou le certificat de l'imprimante.
        self.parc, self.numero, self.repere, self.serie = parc, numero, repere, serie
        memoire = parc.memoire.get(repere) or {}
        self.ip = memoire.get("ip")
        self.emplacements = memoire.get("emplacements") or {}  # "0-1" → {present, couleur, type, …}
        self.attente = {}    # emplacement → {depart : présence avant le premier changement, dernier : heure}
        self.etat = "recherche"
        self.vue = {}        # ce que l'imprimante a annoncé de l'impression, messages partiels réunis
        self.tache = None    # l'impression en cours : {ident, percent, bacs, liee, debut, direct, etat, actif, vu}
        self.bac_actif = None
        self.dernier = 0.0   # heure du dernier départ ou de la dernière fin d'impression vus sur cette imprimante
        self.coupures = []   # emplacements vidés pendant l'impression, notés hors du verrou des emplacements
        self.erreurs = {}    # « hms », « impression » → codes d'erreur que l'imprimante signale en ce moment
        self.verrou_carte = threading.Lock()  # une seule lecture de la carte à la fois
        self.arret = threading.Event()  # levé quand l'imprimante n'est plus à suivre

    def noter(self, etat, detail=""):
        if etat != self.etat:
            print("imprimante %d :" % self.numero, etat, detail, flush=True)
        self.etat = etat

    def designation(self, cle):
        if len(self.parc.imprimantes) > 1:
            return "%s, emplacement %s" % (self.parc.nom(self.repere), nom_emplacement(cle))
        return "Emplacement %s" % nom_emplacement(cle)

    def run(self):
        while not self.arret.is_set():
            pause = 30
            try:
                code = self.parc.code(self)
                if not code:
                    self.noter("sans_code")
                    pause = 300
                else:
                    self.joindre(code)
            except Refus as e:
                self.noter("refusee", str(e))
                pause = 300
            fin = time.time() + pause
            while time.time() < fin and not self.arret.is_set():  # les changements en attente s'annoncent même liaison coupée
                self.verifier()
                time.sleep(2)

    def joindre(self, code):
        """Essaie les adresses connues ; ne rend la main que lorsque la liaison tombe."""
        saisie = adresse_saisie((lire_acces(self.parc.donnees).get(self.repere) or {}).get("adresse"))
        imposee = self.parc.adresses.get(self.serie) or self.parc.adresses.get("*")
        if saisie:  # ajoutée à la main : son adresse, puis celles où Bambu Studio est relié, si elle en a changé
            candidats = [saisie] + [(h, 8883) for h in ([] if self.parc.adresses else adresses_vues()) if h != saisie[0]]
        elif imposee:
            candidats = [imposee]
        else:
            candidats = [(h, 8883) for h in dict.fromkeys(([self.ip] if self.ip else []) + adresses_vues())]
        if not candidats:
            self.noter("injoignable", "adresse inconnue : ouvrir Bambu Studio une fois")
        for hote, port in candidats:
            if self.arret.is_set():
                return
            try:
                self.ecouter(hote, port, code)
            except AutreAppareil:
                if len(candidats) == 1:
                    self.noter("injoignable", "un autre appareil répond à cette adresse")
            except (OSError, ssl.SSLError, ValueError, struct.error) as e:
                # Sur Mac, « No route to host » alors qu'on est sur le réseau : refus de macOS (Réseau local).
                self.noter("bloquee" if MAC and getattr(e, "errno", None) == 65 else "injoignable", str(e))
        if self.etat == "recherche":
            self.noter("injoignable", "imprimante introuvable aux adresses connues")

    def ecouter(self, hote, port, code):
        liaison, flux, serie = ouvrir_liaison(hote, port, code, "blokbambu-%d-%d" % (os.getpid(), self.numero),
                                              self.parc.ac, self.serie or self.repere)
        with liaison:
            self.serie = serie
            if hote != self.ip:
                self.ip = hote
                self.parc.garder()
                self.parc.adresse_trouvee(self.repere, hote, port)
            liaison.sendall(paquet_mqtt(0x82, struct.pack("!H", 1) + chaine_mqtt("device/%s/report" % serie) + b"\x00"))
            liaison.settimeout(5)
            entendu = ping = time.time()
            while not self.arret.is_set():
                recu = flux.paquet()
                maintenant = time.time()
                if recu:
                    entendu = maintenant
                    entete, corps = recu
                    if entete == 0x90:
                        self.noter("connectee")
                    elif entete & 0xF0 == 0x30:
                        debut = 2 + struct.unpack("!H", corps[:2])[0] + (2 if entete & 0x06 else 0)
                        try:
                            self.message(json.loads(corps[debut:].decode("utf-8", "replace")))
                        except ValueError:
                            pass
                elif maintenant - entendu > 90:
                    raise ConnectionError("l'imprimante ne répond plus")
                if maintenant - ping > 25:
                    liaison.sendall(b"\xC0\x00")  # simple signe de vie, pas une commande
                    ping = maintenant
                self.verifier()

    def message(self, donnees):
        p = donnees.get("print") if isinstance(donnees, dict) else None
        if not isinstance(p, dict):
            return
        with self.parc.ecriture:  # la page lit ces emplacements depuis un autre fil d'exécution
            memoire = json.dumps(self.emplacements, sort_keys=True)
            if isinstance(p.get("vt_tray"), dict):
                self.lire_bac(EXTERNE, p["vt_tray"])
            if isinstance(p.get("ams"), dict):
                self.lire_ams(p["ams"])
            if json.dumps(self.emplacements, sort_keys=True) != memoire:
                self.parc.garder()
        coupures, self.coupures = self.coupures, []
        t = self.tache
        for bac, percent in coupures if t else []:
            self.reporter(t["ident"], lambda entree, bobines, bac=bac, percent=percent: noter_coupure(entree, bac, percent))
        try:
            self.noter_bac()
            self.suivre_impression(p)
            self.lire_erreurs(p)
            if self.parc.historique and self.parc.historique.get("etat") == "en attente":  # au repos à présent ?
                self.parc.historique_si_possible()
        except (KeyError, TypeError, ValueError) as e:
            print("impression mal comprise :", repr(e), flush=True)

    def lire_erreurs(self, p):
        """Les erreurs que l'imprimante signale : ses alertes (HMS) et ce qui met l'impression en pause.
        Une fenêtre pour chaque nouvelle ; celles déjà là quand l'écoute commence sont seulement affichées."""
        lues = {}
        if isinstance(p.get("hms"), list):
            lues["hms"] = set()
            for h in p["hms"]:
                try:
                    lues["hms"].add("%08X%08X" % (int(h["attr"]), int(h["code"])))
                except (KeyError, TypeError, ValueError):
                    continue
        if "print_error" in p:
            try:
                n = int(p["print_error"])
            except (TypeError, ValueError):
                n = 0
            # Une impression annulée ou finie n'est pas une erreur à signaler : la fin d'impression le dit.
            lues["impression"] = {"%08X" % n} if n and self.vue.get("gcode_state") in self.EN_COURS else set()
        nouvelles = []
        for nature, codes in lues.items():
            if nature in self.erreurs:
                nouvelles += sorted(codes - self.erreurs[nature])
            self.erreurs[nature] = codes
        if not nouvelles:
            return
        textes = textes_erreurs(self.serie, self.parc.conf)
        qui = "« %s »" % self.parc.nom(self.repere) if len(self.parc.imprimantes) > 1 else "L'imprimante"
        lignes = ["%s (%s)" % (textes[c], code_lisible(c)) if c in textes else "Erreur %s." % code_lisible(c) for c in nouvelles]
        print("erreur signalée :", ", ".join(code_lisible(c) for c in nouvelles), flush=True)
        self.parc.fenetres.montrer("%s signale un problème.\n%s" % (qui, "\n".join(lignes)),
                                   "Le détail et la marche à suivre sont aussi dans Bambu Studio et Bambu Handy.")

    def lire_bac(self, cle, bac):
        """Retient ce qu'un emplacement dit de sa bobine : fil, puce, reste."""
        e = self.emplacements.setdefault(cle, {})
        if "tray_type" in bac:
            e["type"] = bac["tray_type"] or None
        if "tray_color" in bac:
            e["couleur"] = couleur_propre(bac["tray_color"]) if bac["tray_color"] and e.get("type") else None
        if "tray_info_idx" in bac:
            e["indice"] = bac["tray_info_idx"] or None
        if "tag_uid" in bac:  # une puce : seules les bobines Bambu Lab en portent
            e["puce"] = bool(str(bac["tag_uid"]).strip("0"))
        for champ, nom in (("remain", "remain"), ("tray_weight", "poids_puce")):
            if champ in bac:
                try:
                    e[nom] = float(bac[champ])
                except (TypeError, ValueError):
                    e[nom] = None
        if cle == EXTERNE:
            e["present"] = bool(e.get("type"))

    def lire_ams(self, ams):
        avant = {c: e.get("present") for c, e in self.emplacements.items()}
        for unite in ams.get("ams") or []:
            for rang, bac in enumerate(unite.get("tray") or []):
                try:  # un emplacement vide est annoncé sans numéro : son rang le donne
                    self.lire_bac("%d-%d" % (int(unite.get("id", 0)), int(bac.get("id", rang))), bac)
                except (TypeError, ValueError):
                    continue
        try:
            bits = int(str(ams["tray_exist_bits"]), 16)
        except (KeyError, ValueError):
            bits = None
        if bits is not None:
            unites = ({0} | {int(c.partition("-")[0]) for c in self.emplacements if c != EXTERNE}
                      | {i // 4 for i in range(16) if bits >> i & 1})
            for u in unites:
                for b in range(4):
                    self.emplacements.setdefault("%d-%d" % (u, b), {})["present"] = bool(bits >> (u * 4 + b) & 1)
        for cle, e in self.emplacements.items():
            ancien = avant.get(cle)
            if cle != EXTERNE and ancien is not None and e.get("present") is not None and e["present"] != ancien:
                self.attente.setdefault(cle, {"depart": ancien})["dernier"] = time.time()
                if not e["present"] and self.tache and cle in self.tache["bacs"]:  # bobine finie ou retirée en pleine impression
                    self.coupures.append((cle, self.tache["percent"]))
                print("emplacement", nom_emplacement(cle), ":", "chargé" if e["present"] else "retiré", flush=True)
        if "tray_now" in ams:  # l'emplacement qui alimente la buse : 255 = aucun, 254 = bobine externe
            try:
                n = int(ams["tray_now"])
            except (TypeError, ValueError):
                n = 255
            self.bac_actif = EXTERNE if n == 254 else "%d-%d" % (n // 4, n % 4) if 0 <= n < 16 else None

    # ----- l'impression en cours

    def suivre_impression(self, p):
        premier = "gcode_state" not in self.vue
        for champ in ("gcode_state", "mc_percent", "subtask_id", "task_id", "subtask_name", "gcode_file", "gcode_start_time"):
            if champ in p:
                self.vue[champ] = p[champ]
        etat = self.vue.get("gcode_state")
        nom = str(self.vue.get("subtask_id") or self.vue.get("task_id") or "")
        if nom in ("", "0"):  # une impression lancée hors du nuage de Bambu n'a pas de numéro : son nom en tient lieu
            nom = str(self.vue.get("subtask_name") or self.vue.get("gcode_file") or "")
        if not etat or not nom:
            return
        ident = "%s:%s" % (self.repere, nom)
        percent = max(min(int(float(self.vue.get("mc_percent") or 0)), 100), 0)
        maintenant = time.time()
        if etat in self.EN_COURS:
            if not self.tache or self.tache["ident"] != ident:
                # `direct` : on a vu l'impression démarrer, et non découvert une impression déjà lancée
                self.tache = {"ident": ident, "percent": percent, "bacs": [], "liee": False, "debut": maintenant,
                              "direct": not premier, "etat": etat, "actif": 0.0, "vu": maintenant, "apres": self.dernier,
                              "vus": {}}
                self.dernier = maintenant
                self.lier()
            t = self.tache
            if t["etat"] != "PAUSE":  # le temps d'impression ne compte pas les pauses
                t["actif"] += maintenant - t["vu"]
            t["vu"], t["etat"], t["percent"] = maintenant, etat, percent
            if etat == "RUNNING":
                self.noter_bac()
        elif etat in ("FINISH", "FAILED"):
            tache, self.tache = (self.tache if self.tache and self.tache["ident"] == ident else None), None
            self.dernier = maintenant
            if tache and tache["etat"] != "PAUSE":
                tache["actif"] += maintenant - tache["vu"]
            self.conclure(ident, etat, tache["percent"] if tache else percent, tache["bacs"] if tache else [],
                          tache["actif"] if tache and tache["direct"] else None, annoncer=not premier)
        else:
            self.tache = None

    def fils_utilises(self, bacs, bobines, premiers=None):
        """Ce qui alimentait les emplacements vus en service : la bobine notée à leur première utilisation,
        sinon celle posée là sur la page, sinon le fil annoncé."""
        occupe = occupation(self.parc.places(), bobines, annonces_sures=False)
        fils = []
        for bac in bacs:
            o = (premiers or {}).get(bac) or occupe.get("%s:%s" % (self.repere, bac))
            if o:
                couleur, _, type_fil = o["cle"].partition("|")
                fils.append({"couleur": couleur, "type": type_fil, "bobine": o.get("bobine"), "bac": bac})
        return fils

    def noter_bac(self):
        """Note l'emplacement qui alimente l'impression en cours et la bobine qui s'y trouve. Si elle change
        en cours de route, la suite de l'impression revient à celle qui prend le relais."""
        t = self.tache
        if not t or self.vue.get("gcode_state") != "RUNNING" or not self.bac_actif:
            return
        bac, bobines = self.bac_actif, self.parc.bobines()
        o = occupation(self.parc.places(), bobines, annonces_sures=False).get("%s:%s" % (self.repere, bac))
        occupant = {"bobine": o["bobine"], "cle": o["cle"]} if o else None
        if bac in t["bacs"] and t["vus"].get(bac, "?") == occupant:
            return
        if bac not in t["bacs"]:
            t["bacs"].append(bac)
        t["vus"][bac] = occupant
        percent = t["percent"]

        def changement(entree, bobines):
            note = noter_occupant(entree, bac, occupant, percent, bobines)
            premiers = (entree.get("suivi") or {}).get("premiers")
            return attribuer_bacs(entree, self.fils_utilises(t["bacs"], bobines, premiers), bobines, False) or note
        self.reporter(t["ident"], changement)

    def reporter(self, ident, changement):
        """Applique un changement à l'impression en cours du journal liée à cette tâche, s'il y en a une."""
        with verrou:
            journal = lire_journal(self.parc.donnees)
            entree = next((e for e in reversed(journal) if e.get("tache") == ident and not e.get("supprimee")
                           and e.get("etat", "lancee") == "lancee"), None)
            if entree and changement(entree, lire_bobines(self.parc.donnees)):
                ecrire_json(self.parc.donnees, "impressions.json", journal)

    def lier(self):
        """Rattache l'impression que fait l'imprimante à celle que Bambu Studio vient de lancer d'ici."""
        t = self.tache
        nom = str(self.vue.get("subtask_name") or "").lower()
        with verrou:
            journal = lire_journal(self.parc.donnees)
            if any(e.get("tache") == t["ident"] and e.get("etat", "lancee") == "lancee" for e in journal):
                t["liee"], t["vus"] = True, {}
                return
            maintenant = datetime.now()
            limite = (maintenant - timedelta(hours=48)).isoformat(timespec="seconds")
            libres = [e for e in comptees(journal) if e.get("etat", "lancee") == "lancee" and not e.get("tache")
                      and e["date"] >= limite and e.get("source", "studio") == "studio"]
            # Bambu Studio nomme l'impression d'après le projet (« projet_plate_1 ») : c'est le lien le plus sûr.
            choisies = [e for e in libres if nom and nom.startswith(e["projet"].lower())]
            if not choisies and t["direct"]:
                # Nom changé à l'envoi : la dernière lancée dans les dix minutes, pourvu que l'imprimante
                # n'ait rien fait d'autre depuis — sinon c'est une impression qui n'est jamais partie.
                recent = max(maintenant - timedelta(minutes=10), datetime.fromtimestamp(t["apres"])).isoformat(timespec="seconds")
                choisies = [e for e in libres if e["date"] >= recent]
            if choisies:
                t["vus"] = {}  # ce qui alimente l'impression sera noté sur elle à la prochaine annonce
                choisies[-1]["tache"] = t["ident"]
                choisies[-1]["nom_imprimante"] = str(self.vue.get("subtask_name") or "")
                if not choisies[-1].get("imprimante_choisie"):
                    choisies[-1]["imprimante"] = self.repere
                ecrire_json(self.parc.donnees, "impressions.json", journal)
                t["liee"] = True
                print("impression suivie :", choisies[-1]["projet"], flush=True)

    def noter_ailleurs(self):
        """Une impression que Bambu Studio n'a pas lancée d'ici (téléphone, écran de l'imprimante, autre
        ordinateur) : elle est notée d'après ce que dit l'imprimante. Ses grammes restent à compléter."""
        t = self.tache
        fichier = str(self.vue.get("gcode_file") or "")
        nom = nom_tache(self.vue.get("subtask_name") or fichier)
        if fichier.startswith("/usr/") or "auto_cali" in (nom + fichier).lower():  # réglage interne, pas une impression
            t["liee"] = True
            return
        debut = t["debut"]
        try:  # quand l'impression tournait déjà, l'imprimante dit parfois à quelle heure elle est partie
            annonce = float(self.vue.get("gcode_start_time") or 0)
            if not t["direct"] and 0 < debut - annonce < 7 * 86400:
                debut = annonce
        except (TypeError, ValueError):
            pass
        entree = {"cle": "imprimante|%s|%d" % (t["ident"], debut), "projet": nom, "fichier": "", "etat": "lancee",
                  "date": datetime.fromtimestamp(debut).isoformat(timespec="seconds"), "source": "imprimante",
                  "a_completer": True, "tache": t["ident"], "imprimante": self.repere, "fils": [],
                  "nom_imprimante": str(self.vue.get("subtask_name") or ""), "fichier_imprimante": fichier}
        with verrou:
            journal = lire_journal(self.parc.donnees)
            if not any(e.get("tache") == t["ident"] and e.get("etat", "lancee") == "lancee" for e in journal):
                bobines = lire_bobines(self.parc.donnees)
                entree["suivi"] = {"premiers": {bac: t["vus"].get(bac) for bac in t["bacs"]}, "coupures": {}}
                attribuer_bacs(entree, self.fils_utilises(t["bacs"], bobines, entree["suivi"]["premiers"]), bobines, False)
                journal.append(entree)
                journal.sort(key=lambda e: e["date"])
                ecrire_json(self.parc.donnees, "impressions.json", journal)
                print("impression lancée ailleurs :", nom, flush=True)
        t["liee"] = True

    def conclure(self, ident, etat, percent, bacs, actif, annoncer):
        """L'impression est finie ou arrêtée : une impression arrêtée ne compte que pour la part faite."""
        finie = []

        def changement(entree, bobines):
            entree["etat"] = "terminee" if etat == "FINISH" else "arretee"
            if etat == "FAILED" and not entree.get("corrigee"):
                entree["part"] = round(percent / 100, 2)
            if actif:
                entree["duree"] = round(actif)
            # Les emplacements notés sur l'impression comptent aussi : l'ordinateur a pu redémarrer en cours de route.
            premiers = (entree.get("suivi") or {}).get("premiers") or {}
            vus = list(dict.fromkeys(list(premiers) + list(bacs)))
            attribuer_bacs(entree, self.fils_utilises(vus, bobines, premiers), bobines, True)
            print("impression", "terminée :" if etat == "FINISH" else "arrêtée à %d %% :" % percent, entree["projet"], flush=True)
            finie.append(dict(entree))
            return True
        self.reporter(ident, changement)
        if not finie:
            return
        entree = finie[0]
        if annoncer and lire_reglages(self.parc.donnees)["fin"]:
            titre = ("« %s » est terminée." % entree["projet"] if etat == "FINISH" else
                     "« %s » s'est arrêtée à %d %%." % (entree["projet"], percent))
            details = [self.parc.nom(self.repere)] if len(self.parc.imprimantes) > 1 else []
            if temps(entree):
                details.append(duree_texte(temps(entree)))
            grammes = sum(f["g"] for f in entree["fils"]) * part(entree)
            if grammes:
                details.append("%s de fil" % poids_texte(grammes))
            a_saisir = entree.get("a_completer") and not lire_reglages(self.parc.donnees)["carte"]
            self.parc.fenetres.montrer(titre + ("\n" + " · ".join(details) if details else ""),
                                       "Le fil utilisé reste à indiquer sur la page." if a_saisir else "")
        if entree.get("a_completer"):
            self.lire_carte(entree)
        elif entree.get("source", "studio") == "studio" and lire_reglages(self.parc.donnees)["carte"] and self.ip and self.serie:
            nom = entree.get("nom_imprimante") or str(self.vue.get("subtask_name") or "")
            threading.Thread(target=self.noter_emplacements, daemon=True,
                             args=(entree["cle"], nom, (entree.get("plateaux") or [1])[0])).start()

    def noter_emplacements(self, cle, nom, plateau):
        """Lit la note que l'imprimante garde de l'impression : de quel emplacement chaque fil est vraiment
        parti. Elle remplace la couleur la plus proche, devinée quand le projet a plusieurs fils."""
        try:
            with self.verrou_carte:
                bacs = lire_note(self.ip, self.parc.port_carte, self.parc.code(self), self.serie, nom, plateau, self.parc.ac)
        except Exception as e:
            print("note de l'imprimante illisible :", type(e).__name__, flush=True)
            return
        if not bacs:
            return
        with verrou:
            journal, bobines = lire_journal(self.parc.donnees), lire_bobines(self.parc.donnees)
            entree = next((e for e in journal if e.get("cle") == cle and not e.get("supprimee")), None)
            if entree and placer_fils(entree, bacs, occupation(self.parc.places(), bobines, annonces_sures=False),
                                      self.repere, bobines):
                ecrire_json(self.parc.donnees, "impressions.json", journal)
                print("emplacements lus sur l'imprimante :", entree["projet"], flush=True)

    def disponible(self):
        """Allumée, reliée, et au repos : sa carte peut se lire sans gêner une impression."""
        return (self.etat == "connectee" and bool(self.ip and self.serie) and not self.tache
                and self.vue.get("gcode_state") not in (None,) + self.EN_COURS)

    def reprendre_historique(self, bilan):
        """Ajoute les impressions passées dont la carte garde le fichier, une seule fois chacune. Lecture
        seule ; on s'arrête si une impression commence."""
        nom = self.parc.nom(self.repere)
        if self.etat != "connectee" or not self.ip or not self.serie:
            bilan["sautees"].append("%s : injoignable" % nom)
            return
        if not self.disponible():
            bilan["sautees"].append("%s : en train d'imprimer" % nom if self.tache else "%s : pas encore au repos" % nom)
            return
        with self.verrou_carte:
            carte = ouvrir_carte(self.ip, self.parc.port_carte, self.parc.code(self), self.serie, self.parc.ac)
            try:
                fichiers = sorted((x for x in carte.lister("/cache") if x[0].lower().endswith(".3mf")), key=lambda x: x[2])
                bilan["total"] += len(fichiers)
                for fichier, taille, date in fichiers:
                    if self.tache:
                        bilan["interrompue"] = True
                        return
                    cle = "carte|%s|%s|%d" % (self.repere, fichier, taille)
                    if taille <= MAXI_CARTE and not any(e.get("cle") == cle for e in lire_journal(self.parc.donnees)):
                        envoi = envoi_du_3mf(carte.lire("/cache/" + fichier))
                        if envoi:
                            bacs = note_carte(carte, "/cache", fichier[:-4], envoi["plateaux"][0])
                            self.ajouter_ancienne(cle, fichier, date, envoi, bacs, bilan)
                    bilan["lus"] += 1
            finally:
                fermer_carte(carte)

    def ajouter_ancienne(self, cle, fichier, date, envoi, bacs, bilan):
        iso = date.isoformat(timespec="seconds")
        entree = {"cle": cle, "date": iso, "projet": nom_tache(fichier), "fichier": "", "etat": "terminee",
                  "source": "carte", "imprimante": self.repere, "secondes": envoi["secondes"],
                  "plateaux": envoi["plateaux"], "fils": [dict(f) for f in envoi["fils"]]}
        for f in entree["fils"]:
            if bacs.get(f.get("numero")):
                f["bac"] = bacs[f["numero"]]
        pesee = sorted((f["couleur"], f["type"], f["g"]) for f in entree["fils"])
        debut = (date - timedelta(minutes=45)).isoformat(timespec="seconds")
        fin = (date + timedelta(minutes=45)).isoformat(timespec="seconds")
        with verrou:
            journal, bobines = lire_journal(self.parc.donnees), lire_bobines(self.parc.donnees)
            if any(meme_impression(e, pesee, debut, fin) for e in journal):  # déjà comptée d'ici
                bilan["deja"] += 1
                return
            # Seules les bobines déjà suivies à cette date peuvent l'avoir fournie : une impression manquée
            # (ordinateur éteint) se déduit d'elles, une impression d'avant BlokBambu ne touche à aucune.
            anciennes = {i: b for i, b in bobines.items() if b.get("depuis", "") <= iso}
            for f in entree["fils"]:
                choix = bobine_probable(cle_fil(f), anciennes, {})
                if choix:
                    f["bobine"] = choix
            poser_prix(entree, bobines)
            journal.append(entree)
            journal.sort(key=lambda e: e["date"])
            ecrire_json(self.parc.donnees, "impressions.json", journal)
        bilan["ajoutees"] += 1

    def lire_carte(self, entree):
        """Va lire sur la carte de l'imprimante le fichier d'une impression lancée ailleurs, pour connaître
        son fil. Une fois l'impression finie, en lecture seule, et seulement si le réglage est activé."""
        if lire_reglages(self.parc.donnees)["carte"] and self.ip and self.serie:
            threading.Thread(target=self.completer, daemon=True, args=(
                entree["cle"], entree.get("nom_imprimante") or "", entree.get("fichier_imprimante") or "")).start()

    def completer(self, cle, nom, fichier):
        try:
            with self.verrou_carte:
                lu = lire_carte(self.ip, self.parc.port_carte, self.parc.code(self), self.serie, nom, fichier, self.parc.ac)
        except Exception as e:  # carte absente, imprimante occupée… : la saisie à la main reste possible
            print("carte de l'imprimante illisible :", type(e).__name__, flush=True)
            return
        if not lu:
            print("fichier introuvable sur la carte de l'imprimante :", nom, flush=True)
            return
        with verrou:
            journal, bobines = lire_journal(self.parc.donnees), lire_bobines(self.parc.donnees)
            entree = next((e for e in journal if e.get("cle") == cle and e.get("a_completer") and not e.get("supprimee")), None)
            if not entree:
                return
            # La même impression, déjà comptée d'après le fichier de Bambu Studio sans avoir pu être
            # reconnue sur l'imprimante (nom changé à l'envoi) : c'est elle qu'on garde.
            pesee = sorted((f["couleur"], f["type"], f["g"]) for f in lu["fils"])
            limite = (datetime.fromisoformat(entree["date"]) - timedelta(hours=48)).isoformat(timespec="seconds")
            jumelle = next((e for e in reversed(journal) if not e.get("supprimee") and not e.get("tache")
                            and e.get("source", "studio") == "studio" and e.get("etat", "lancee") == "lancee"
                            and limite <= e["date"] <= entree["date"]
                            and sorted(((f.get("projet") or f)["couleur"], (f.get("projet") or f)["type"], f["g"])
                                       for f in e["fils"]) == pesee), None)
            if jumelle:
                jumelle.update({k: entree[k] for k in ("tache", "etat", "part", "duree") if k in entree})
                if not jumelle.get("imprimante_choisie"):
                    jumelle["imprimante"] = self.repere
                entree["supprimee"] = True
                entree = jumelle
            else:
                vus = [f["bac"] for f in entree["fils"] if f.get("bac")]
                entree["secondes"] = lu["secondes"]
                entree["fils"] = [dict(f) for f in lu["fils"]]
                entree.pop("a_completer", None)
                if not lu["bacs"]:  # pas de note de l'imprimante : les emplacements vus en service, à la couleur la plus proche
                    attribuer_bacs(entree, self.fils_utilises(vus, bobines), bobines, True)
            # Chaque fil va à la bobine de l'emplacement que l'imprimante a noté pour lui.
            occupe = occupation(self.parc.places(), bobines, annonces_sures=False)
            placer_fils(entree, lu["bacs"], occupe, self.repere, bobines)
            for f in entree["fils"]:
                if not f.get("bobine") and not f.get("bobine_choisie") and not lu["bacs"].get(f.get("numero")):
                    choix = bobine_probable(cle_fil(f), bobines, occupe)
                    if choix:
                        f["bobine"] = choix
            appliquer_changements(entree, bobines)
            poser_prix(entree, bobines)
            ecrire_json(self.parc.donnees, "impressions.json", journal)
        print("fil lu sur la carte de l'imprimante :", entree["projet"], flush=True)

    def verifier(self):
        """Annonce les changements de bobine une fois stabilisés, et relie l'impression en cours si besoin."""
        maintenant, lignes = time.time(), []
        for cle in list(self.attente):
            a = self.attente[cle]
            present = self.emplacements.get(cle, {}).get("present")
            calme = maintenant - a["dernier"]
            if present and calme >= self.parc.stabilisation:
                quoi = "bobine changée" if a["depart"] else "bobine chargée"
            elif not present and calme >= self.parc.remplacement:
                quoi = "bobine retirée ou terminée" if a["depart"] else None
            else:
                continue
            del self.attente[cle]
            if quoi:
                lignes.append("%s : %s." % (self.designation(cle), quoi))
        if lignes:
            self.parc.fenetres.montrer("\n".join(sorted(lignes)), "Pensez à mettre la page des bobines à jour.")
        # L'imprimante peut annoncer le départ avant que le fichier de Bambu Studio ait été lu : on y revient.
        # Passé ce délai, c'est une impression lancée ailleurs.
        t = self.tache
        if t and not t["liee"] and maintenant - t.get("essai", 0) > 5:
            t["essai"] = maintenant
            self.lier()
            if not t["liee"] and maintenant - t["debut"] >= self.parc.attente_studio:
                self.noter_ailleurs()


# ---------------------------------------------------------------- lancement

def main():
    a = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    a.add_argument("--racine", default=None, help="dossier temporaire à surveiller (essais)")
    a.add_argument("--donnees", default=DONNEES, help="dossier du journal et de la page")
    a.add_argument("--port", type=int, default=PORT, help="port de la page sur cet ordinateur")
    a.add_argument("--une-fois", action="store_true", help="deux passages puis arrêt, sans serveur (essais)")
    a.add_argument("--page", action="store_true", help="refaire seulement le raccourci de la page")
    a.add_argument("--couleurs", metavar="DOSSIER", help="relever les couleurs des projets Bambu de ce dossier")
    a.add_argument("--sans-imprimante", action="store_true", help="ne pas écouter les imprimantes")
    a.add_argument("--imprimante", metavar="[SERIE=]ADRESSE:PORT,…", help="adresses imposées des imprimantes (essais)")
    a.add_argument("--bambu-conf", default=None, help="réglages de Bambu Studio à lire (essais)")
    a.add_argument("--bambu-ac", default=None, help="certificat qui reconnaît l'imprimante (essais)")
    a.add_argument("--fenetre-essai", metavar="FICHIER", help="écrire les fenêtres dans ce fichier au lieu de les afficher")
    a.add_argument("--delais", default="%d,%d" % (STABILISATION, REMPLACEMENT), help="délais d'annonce en secondes (essais)")
    a.add_argument("--journal", action="store_true", help="écrire le journal du programme dans le dossier des données")
    a.add_argument("--chemins", action="store_true", help="montrer les dossiers utilisés sur cet ordinateur, puis s'arrêter")
    a.add_argument("--port-carte", type=int, default=990, help="port de la carte mémoire des imprimantes (essais)")
    a.add_argument("--maj", default=DEPOT + "version.txt", metavar="ADRESSE",
                   help="où lire la dernière version parue ; vide : ne pas regarder")
    o = a.parse_args()
    for flux in (sys.stdout, sys.stderr):  # un nom de projet inattendu ne doit jamais faire échouer une écriture
        try:
            flux.reconfigure(errors="backslashreplace")
        except Exception:
            pass
    racines = [o.racine] if o.racine else racines_bambu()
    if o.chemins:
        print(json.dumps({"donnees": o.donnees, "reglages_bambu": o.bambu_conf or conf_bambu(),
                          "dossiers_de_travail": racines, "certificats": certificats_installes()},
                         ensure_ascii=False, indent=1))
        return
    os.makedirs(o.donnees, exist_ok=True)
    reprendre_anciennes_donnees(o.donnees)
    if o.journal:  # sans launchd pour le faire (Windows, Linux), le programme tient lui-même son journal
        sys.stdout = sys.stderr = open(os.path.join(o.donnees, "compteur.log"), "a", encoding="utf-8", buffering=1)
    if o.couleurs:
        print("couleurs relevées :", len(relever_couleurs(o.couleurs, o.donnees)))
    ecrire_page(o.donnees, o.port)
    if o.page or o.couleurs:
        return
    fenetres = Fenetres(o.port, o.donnees, o.fenetre_essai)
    veilleur = Veilleur(racines, o.donnees, fenetres)
    print("surveillance de", ", ".join(os.path.join(r, "bamboo_model") for r in racines), flush=True)
    if o.une_fois:
        veilleur.passage()
        time.sleep(0.1)
        print("ajouts :", veilleur.passage())
        return
    if not MAC and deja_lance(o.port):  # sur Mac, launchd ne lance qu'un exemplaire
        print("déjà lancé : cet exemplaire s'arrête", flush=True)
        return
    parc = None
    if not o.sans_imprimante:
        adresses = {}
        for morceau in filter(None, (o.imprimante or "").split(",")):
            serie, _, adresse = morceau.rpartition("=")
            hote, _, port_imprimante = adresse.rpartition(":")
            adresses[serie or "*"] = (hote, int(port_imprimante))
        parc = Parc(o.donnees, fenetres, o.bambu_conf, o.bambu_ac, adresses, tuple(float(x) for x in o.delais.split(",")))
        parc.port_carte = o.port_carte
    nouveautes = None
    if o.maj and version_installee():
        nouveautes = Nouveautes(o.maj)
        nouveautes.start()
    serveur, ouvert, essai, vu = None, None, 0, 0
    while True:
        maintenant = time.time()
        if parc and maintenant - vu > 60:  # une imprimante ajoutée dans Bambu Studio est écoutée dans la minute
            vu = maintenant
            parc.actualiser()
            rattacher(o.donnees)
            parc.historique_si_possible()
        reseau = lire_reglages(o.donnees)["reseau"]
        if serveur is not None and ouvert != reseau:  # le réglage a changé : la page change de porte
            serveur.shutdown()
            serveur.server_close()
            serveur = None
        if serveur is None and maintenant >= essai:
            try:
                serveur, ouvert = lancer_serveur(o.donnees, o.port, parc, reseau, nouveautes), reseau
                print("page sur http://127.0.0.1:%d/%s" % (o.port, ", ouverte au réseau local" if reseau else ""), flush=True)
            except OSError as e:  # port occupé : la surveillance continue, on réessaie plus tard
                print("page indisponible :", e, file=sys.stderr, flush=True)
                essai = maintenant + 30
        try:
            if veilleur.passage() and parc:
                rattacher(o.donnees)
        except Exception as e:  # ne jamais s'arrêter sur un fichier inattendu
            print("erreur :", repr(e), file=sys.stderr, flush=True)
        time.sleep(PAUSE)


# Certificats de l'autorité de Bambu Lab, tels que Bambu Studio les livre (resources/cert/printer.cer).
# Ils servent à vérifier que l'appareil joint est bien une imprimante Bambu avant de lui présenter le code.
AC_BAMBU = """
-----BEGIN CERTIFICATE-----
MIIFfzCCA2egAwIBAgIUXtzR6tRiL/RHBRXOoyFU0+XrliowDQYJKoZIhvcNAQEL
BQAwRjELMAkGA1UEBhMCQ04xITAfBgNVBAoMGEJCTCBUZWNobm9sb2dpZXMgQ28u
IEx0ZDEUMBIGA1UEAwwLQkJMIENBMiBSU0EwIBcNMjUwNjE3MDEzODA4WhgPMjA1
MDA2MTcwMTM4MDhaMEYxCzAJBgNVBAYTAkNOMSEwHwYDVQQKDBhCQkwgVGVjaG5v
bG9naWVzIENvLiBMdGQxFDASBgNVBAMMC0JCTCBDQTIgUlNBMIICIjANBgkqhkiG
9w0BAQEFAAOCAg8AMIICCgKCAgEAo4550G4c42gTKzQqixwKT089RizIdZpyOcGA
679rPaOdWsMqVwnYPP2FpMqXKkjFbedE+SpGloi2NKCuiPNVRbq9PHOOZwTs7YLo
bOwf53FJuO6vRFpzFfX1tlc9zlFqJvZnYO9NgHpMysidocWcgrDN/SIDywgPB5CV
bYg3Vvzua9fwZx9e5KT9xd5IpTqdTrWS47jQOVKLhdQCbJFIlMrblOwLBAx+fHok
wqh6tkI6Ktuyyjw8Dysebi1ndWjKtZ2mW47r8xZ/J+z3EZqcyJMY6MRtx/zb1jBF
uHtkjrb5Kv1DMzSKlkaNJIbvC+Mk+hI97W+SjLSRuIdC7+oJUzWaSzgu9cjXCVfm
q8t4IL/35hP69PK95LgLectIrP96CYAT/aVMG19FrFW0QWEyfT+kzG4jkumfPbHq
Y2nNkEN0+tjj3h4WdzrWgQEojK/lhfcRFVkts74+aZoMpQP+vmL17CKmSzXk5o/e
K21xgxJdzMbdztfTpibiXk0abfOpN+1VR+3NYa+bROAKNyGaReEGsyW2bjcjNx51
5Vqzj3SVxhMSp5vfF9E4A1jE99M/l9jQDM6RzkT0lMccGAd5tUSdNvDlrqtQaQiK
v/ZsXPgXLTWfOpvaLNEgwdMgZMuhjpkwvAZyoYfeF9kyydjDh7bvrX//cz/VopAU
lxUtQtMCAwEAAaNjMGEwHQYDVR0OBBYEFNVJgQad1sNTN0jxVkwbJ/XM1an1MB8G
A1UdIwQYMBaAFNVJgQad1sNTN0jxVkwbJ/XM1an1MA8GA1UdEwEB/wQFMAMBAf8w
DgYDVR0PAQH/BAQDAgEGMA0GCSqGSIb3DQEBCwUAA4ICAQBFZDKMJfp/N4gBeFHh
MiFehaUyMS6e9mzrTfMLJLJoj6Jopa9V9jIfcCEBGZuRThqFcATV+UdFHSINpUcH
upcCYnazTRC4dn1hnxnQ1ojQcHxdGp9xGw/YclAKD97d8bPShfBMT1to9zbMK7T5
L8zgqg01YIOKjQk0Hcd0+0iUr6m8zQ5P8Rl3QXqAyeWgqmYQrrjTWwPsgdfHNXKX
vDrx7/cqry5lKU802hUplKMBxelv4W8407Ytj1lfJOwvxqxxsFU5jSwcUG3zo2vk
QtjRs8m5BKup5K1OPYkkPu7Ld89X0XpU073/dNDG11uxb1eDKrtNP6vZuZjNE2Pq
8HCoI1EtP+ItyqtUMvHi6Z2zsmlA25broVioeUKxjlIecpQ9JR/FhDu9CWNF/nDW
LSORNaMMzgsMSzI+HCiUhqN+qMIvVP6rzGTJzwqz/lc5Lf+ZPCnGA9WJTT4uPIhf
ufbZmnUJ35WuWKHxovDsqBh88zQ9sZ+ei4Hi4vVzOhUgfG3aLoSQEYqRoqaboANh
wCwzyuW2Rv54u5QSBbd6Gx1OpvsWmLPWd2/iL2kISl5wfmLGVydvSJa+rbOfuAy7
ycVQacVDQCAnbhoVrQy7+454QsKSW3ZV6BcyRrorewCyCYgd7nyxflxHZTBEykXX
haGNe/KFNvJBMOIuIUzknRRmiQ==
-----END CERTIFICATE-----
-----BEGIN CERTIFICATE-----
MIIB8zCCAZmgAwIBAgIUe61jGQ4RzIC8k+sNuqbI/CaNqPIwCgYIKoZIzj0EAwIw
RjELMAkGA1UEBhMCQ04xITAfBgNVBAoMGEJCTCBUZWNobm9sb2dpZXMgQ28uIEx0
ZDEUMBIGA1UEAwwLQkJMIENBMiBFQ0MwIBcNMjUwNjE3MDEzODM1WhgPMjA1MDA2
MTcwMTM4MzVaMEYxCzAJBgNVBAYTAkNOMSEwHwYDVQQKDBhCQkwgVGVjaG5vbG9n
aWVzIENvLiBMdGQxFDASBgNVBAMMC0JCTCBDQTIgRUNDMFkwEwYHKoZIzj0CAQYI
KoZIzj0DAQcDQgAEpKTF7wRSty4DXpGJzgCPwRh8ghLlxUC3qJbyEgLqTvJgbiwY
APPHK7kVbVmerkqhHOT4QeWRlTG3dOQGLA2VpaNjMGEwHQYDVR0OBBYEFKuRpsjY
REOyIKH7HwOE6jhGBd6NMB8GA1UdIwQYMBaAFKuRpsjYREOyIKH7HwOE6jhGBd6N
MA8GA1UdEwEB/wQFMAMBAf8wDgYDVR0PAQH/BAQDAgEGMAoGCCqGSM49BAMCA0gA
MEUCIErBiUm3VdtP3rz4kb8aLpI5p+BzL7M9vElBGWWJxpHMAiEA3r5tJWVGwuxi
YCrB1c40KYFRFyahGrhOJZAj/YhRdnU=
-----END CERTIFICATE-----
-----BEGIN CERTIFICATE-----
MIIEeTCCA2GgAwIBAgIUOq+lNIaC2xsswkFqj5JPyVBl45cwDQYJKoZIhvcNAQEL
BQAwQjELMAkGA1UEBhMCQ04xIjAgBgNVBAoMGUJCTCBUZWNobm9sb2dpZXMgQ28u
LCBMdGQxDzANBgNVBAMMBkJCTCBDQTAeFw0yNTA2MTcwMjAxMjdaFw0zNTA2MTUw
MjAxMjdaMEYxCzAJBgNVBAYTAkNOMSEwHwYDVQQKDBhCQkwgVGVjaG5vbG9naWVz
IENvLiBMdGQxFDASBgNVBAMMC0JCTCBDQTIgUlNBMIICIjANBgkqhkiG9w0BAQEF
AAOCAg8AMIICCgKCAgEAo4550G4c42gTKzQqixwKT089RizIdZpyOcGA679rPaOd
WsMqVwnYPP2FpMqXKkjFbedE+SpGloi2NKCuiPNVRbq9PHOOZwTs7YLobOwf53FJ
uO6vRFpzFfX1tlc9zlFqJvZnYO9NgHpMysidocWcgrDN/SIDywgPB5CVbYg3Vvzu
a9fwZx9e5KT9xd5IpTqdTrWS47jQOVKLhdQCbJFIlMrblOwLBAx+fHokwqh6tkI6
Ktuyyjw8Dysebi1ndWjKtZ2mW47r8xZ/J+z3EZqcyJMY6MRtx/zb1jBFuHtkjrb5
Kv1DMzSKlkaNJIbvC+Mk+hI97W+SjLSRuIdC7+oJUzWaSzgu9cjXCVfmq8t4IL/3
5hP69PK95LgLectIrP96CYAT/aVMG19FrFW0QWEyfT+kzG4jkumfPbHqY2nNkEN0
+tjj3h4WdzrWgQEojK/lhfcRFVkts74+aZoMpQP+vmL17CKmSzXk5o/eK21xgxJd
zMbdztfTpibiXk0abfOpN+1VR+3NYa+bROAKNyGaReEGsyW2bjcjNx515Vqzj3SV
xhMSp5vfF9E4A1jE99M/l9jQDM6RzkT0lMccGAd5tUSdNvDlrqtQaQiKv/ZsXPgX
LTWfOpvaLNEgwdMgZMuhjpkwvAZyoYfeF9kyydjDh7bvrX//cz/VopAUlxUtQtMC
AwEAAaNjMGEwDwYDVR0TAQH/BAUwAwEB/zAOBgNVHQ8BAf8EBAMCAQYwHQYDVR0O
BBYEFNVJgQad1sNTN0jxVkwbJ/XM1an1MB8GA1UdIwQYMBaAFI80QmjcZ06PxCKe
xXxJ5avdRL4eMA0GCSqGSIb3DQEBCwUAA4IBAQAvS8tyfagaGsFf9YncA2ko/Na5
9BVF+8TlUo+32oznwIVpS1AhSgLP6rNVekXNFKbuP5htudLQ17ZRBJI/UMVyYEDq
IN7xv7Zj+zJwF6W6haYrjb2Vk8igw1XvNULZfvVNNKIkvJUiVqEslWrC+k74crk/
Wv8ChVf+zqvfIN6LV3esaGRL02J3AprQGb7DDhR1EefQMScDkNpGJMUmvCmfknrl
iK8qgvQN1SWO7JRf6fNKHsN1ZQvyP0pgLWxpT3V0/0/WttqX3cMGuJF+jVUzm/Nh
xYhFewG8vc3KzTjnwQApMA6CW554FOJWFyOD2jn5yJLT3Vue+aYDQRp4bKMx
-----END CERTIFICATE-----
-----BEGIN CERTIFICATE-----
MIICrjCCAZagAwIBAgIUOq+lNIaC2xsswkFqj5JPyVBl45gwDQYJKoZIhvcNAQEL
BQAwQjELMAkGA1UEBhMCQ04xIjAgBgNVBAoMGUJCTCBUZWNobm9sb2dpZXMgQ28u
LCBMdGQxDzANBgNVBAMMBkJCTCBDQTAeFw0yNTA2MTcwMjAxNDdaFw0zNTA2MTUw
MjAxNDdaMEYxCzAJBgNVBAYTAkNOMSEwHwYDVQQKDBhCQkwgVGVjaG5vbG9naWVz
IENvLiBMdGQxFDASBgNVBAMMC0JCTCBDQTIgRUNDMFkwEwYHKoZIzj0CAQYIKoZI
zj0DAQcDQgAEpKTF7wRSty4DXpGJzgCPwRh8ghLlxUC3qJbyEgLqTvJgbiwYAPPH
K7kVbVmerkqhHOT4QeWRlTG3dOQGLA2VpaNjMGEwDwYDVR0TAQH/BAUwAwEB/zAO
BgNVHQ8BAf8EBAMCAQYwHQYDVR0OBBYEFKuRpsjYREOyIKH7HwOE6jhGBd6NMB8G
A1UdIwQYMBaAFI80QmjcZ06PxCKexXxJ5avdRL4eMA0GCSqGSIb3DQEBCwUAA4IB
AQCg6PjUSSZV+4bvejcVMvgXmKzfD95osWn0ctnoMBxPDa+m+Gg+BcLT2IlFAe3E
KYMvu4T295WQc92rjKYqW6cirFppng9uEFW2mZLimxaSmutsTftE3sbMVMJ/SLYN
PV7TFv6mcBSIFWXwmBOIpbh4BUcVfONTvdSfIqfyAVxsq4xzc2nc6hPBpAm21Ayj
ToC1ev/TbDJ8VllFZiEVmWWlIP3aNzAm8S2mOpxPB2WnanaZHSrvXLFhstyzwrjD
yO1/isOZ7wtr7rcuTJdEvvvCimOZlkfRhaDoTew9tQ0E2FVpzzSinw02qmQ1xIE9
5/H5ZzJSPkpeAHWEPnKkxg0v
-----END CERTIFICATE-----
-----BEGIN CERTIFICATE-----
MIIDZTCCAk2gAwIBAgIUV1FckwXElyek1onFnQ9kL7Bk4N8wDQYJKoZIhvcNAQEL
BQAwQjELMAkGA1UEBhMCQ04xIjAgBgNVBAoMGUJCTCBUZWNobm9sb2dpZXMgQ28u
LCBMdGQxDzANBgNVBAMMBkJCTCBDQTAeFw0yMjA0MDQwMzQyMTFaFw0zMjA0MDEw
MzQyMTFaMEIxCzAJBgNVBAYTAkNOMSIwIAYDVQQKDBlCQkwgVGVjaG5vbG9naWVz
IENvLiwgTHRkMQ8wDQYDVQQDDAZCQkwgQ0EwggEiMA0GCSqGSIb3DQEBAQUAA4IB
DwAwggEKAoIBAQDL3pnDdxGOk5Z6vugiT4dpM0ju+3Xatxz09UY7mbj4tkIdby4H
oeEdiYSZjc5LJngJuCHwtEbBJt1BriRdSVrF6M9D2UaBDyamEo0dxwSaVxZiDVWC
eeCPdELpFZdEhSNTaT4O7zgvcnFsfHMa/0vMAkvE7i0qp3mjEzYLfz60axcDoJLk
p7n6xKXI+cJbA4IlToFjpSldPmC+ynOo7YAOsXt7AYKY6Glz0BwUVzSJxU+/+VFy
/QrmYGNwlrQtdREHeRi0SNK32x1+bOndfJP0sojuIrDjKsdCLye5CSZIvqnbowwW
1jRwZgTBR29Zp2nzCoxJYcU9TSQp/4KZuWNVAgMBAAGjUzBRMB0GA1UdDgQWBBSP
NEJo3GdOj8QinsV8SeWr3US+HjAfBgNVHSMEGDAWgBSPNEJo3GdOj8QinsV8SeWr
3US+HjAPBgNVHRMBAf8EBTADAQH/MA0GCSqGSIb3DQEBCwUAA4IBAQABlBIT5ZeG
fgcK1LOh1CN9sTzxMCLbtTPFF1NGGA13mApu6j1h5YELbSKcUqfXzMnVeAb06Htu
3CoCoe+wj7LONTFO++vBm2/if6Jt/DUw1CAEcNyqeh6ES0NX8LJRVSe0qdTxPJuA
BdOoo96iX89rRPoxeed1cpq5hZwbeka3+CJGV76itWp35Up5rmmUqrlyQOr/Wax6
itosIzG0MfhgUzU51A2P/hSnD3NDMXv+wUY/AvqgIL7u7fbDKnku1GzEKIkfH8hm
Rs6d8SCU89xyrwzQ0PR853irHas3WrHVqab3P+qNwR0YirL0Qk7Xt/q3O1griNg2
Blbjg3obpHo9
-----END CERTIFICATE-----
"""

if __name__ == "__main__":
    main()
