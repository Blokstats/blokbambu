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
import glob
import hashlib
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


def certificats_installes():
    """Le certificat livré avec Bambu Studio, là où son emplacement est connu : il suit ses mises à jour."""
    if MAC:
        bases = ["/Applications/BambuStudio.app/Contents/Resources",
                 os.path.expanduser("~/Applications/BambuStudio.app/Contents/Resources")]
    elif WINDOWS:
        bases = [os.path.join(os.environ[v], "Bambu Studio", "resources")
                 for v in ("ProgramFiles", "ProgramFiles(x86)") if os.environ.get(v)]
    else:
        bases = []
    return [c for c in (os.path.join(b, "cert", "printer.cer") for b in bases) if os.path.exists(c)]


DONNEES = dossier_donnees()
PAGE = "BlokBambu.html"
PORT = 47815
PAUSE = 3  # secondes entre deux passages
BOBINE_NEUVE = 1000  # grammes
PRESQUE_VIDE = 100   # grammes : en dessous, la bobine est signalée
TYPES = ["PLA", "PETG", "ABS", "ASA", "TPU", "PA", "PC"]
EXTERNE = "254"  # le numéro que l'imprimante donne à la bobine posée hors de l'AMS

# L'imprimante : Bambu Studio garde son numéro de série et son code d'accès ; un certificat de
# Bambu Lab (AC_BAMBU, en fin de fichier) permet de la reconnaître avant de lui présenter ce code.
STABILISATION = 20   # secondes sans changement avant d'annoncer une bobine chargée
REMPLACEMENT = 180   # secondes : une bobine retirée puis remise dans ce délai est une bobine changée
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
    presets = reglages.get("filament_settings_id") or []

    plateaux = []
    for p in racine.findall("plate"):
        meta = {m.get("key"): m.get("value") for m in p.findall("metadata")}
        plateaux.append((meta, p))
    # Un projet à plusieurs plateaux peut décrire des plateaux déjà tranchés mais non envoyés :
    # seuls ceux dont le g-code est dans le fichier partent à l'imprimante.
    envoyes = [(m, p) for m, p in plateaux if "Metadata/plate_%s.gcode" % m.get("index") in noms]
    if envoyes:
        plateaux = envoyes

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
            fil = fils.setdefault(cle, {"couleur": cle[0], "type": cle[1], "nom": nom, "g": 0.0, "m": 0.0})
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
    """{"#RRGGBB|TYPE": {couleur, type, poids, depuis}} — une bobine suivie par couleur et type de fil."""
    return lire(donnees, "bobines.json", {})


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


def restes(journal, bobines):
    """Ce qu'il reste sur chaque bobine suivie, d'après les impressions comptées depuis sa pose."""
    r = {cle: float(b["poids"]) for cle, b in bobines.items()}
    for entree in comptees(journal):
        for f in entree["fils"]:
            cle = cle_fil(f)
            if cle in r and entree["date"] >= bobines[cle]["depuis"]:
                r[cle] -= f["g"] * part(entree)
    return r


def poser_prix(entree, bobines):
    """Note sur chaque fil le prix au kilo de sa bobine : le coût d'une impression ne bouge plus ensuite."""
    for f in entree["fils"]:
        b = bobines.get(cle_fil(f))
        if b and b.get("prix") and entree["date"] >= b["depuis"]:
            f["prix_kg"] = b["prix"]
        else:
            f.pop("prix_kg", None)


def cout(entree):
    """Coût du fil d'une impression, ou None si aucun de ses fils n'a de prix."""
    prix = [f["g"] * part(entree) * f["prix_kg"] / 1000 for f in entree["fils"] if f.get("prix_kg")]
    return round(sum(prix), 2) if prix else None


def poids_texte(g):
    if g >= 1000:
        return ("%.2f" % (g / 1000)).rstrip("0").rstrip(".").replace(".", ",") + " kg"
    return "%d g" % round(g)


def ecart(couleur_a, couleur_b):
    try:
        a, b = ([int(c[i:i + 2], 16) for i in (1, 3, 5)] for c in (couleur_a, couleur_b))
    except ValueError:
        return 10 ** 9
    return sum((x - y) ** 2 for x, y in zip(a, b))


def attribuer_bacs(entree, utilises, bobines, fin):
    """Rattache les fils d'une impression aux bobines qui ont vraiment fourni.

    `utilises` : les fils (couleur, type) des emplacements vus en service pendant l'impression.
    L'imprimante ne dit pas quel fil du projet va sur quel emplacement : avec plusieurs fils, on
    attend la fin et on prend, pour chacun, l'emplacement utilisé de la couleur la plus proche.
    """
    fils = entree["fils"]
    if not utilises or (len(fils) > 1 and (not fin or len(utilises) < len(fils))):
        return False
    change = False
    for f in fils:
        origine = f.get("projet") or {"couleur": f["couleur"], "type": f["type"]}
        if len(fils) == 1:
            choix = utilises[0]
        else:
            memes = [u for u in utilises if u["type"] == origine["type"]] or utilises
            choix = min(memes, key=lambda u: ecart(u["couleur"], origine["couleur"]))
        if (choix["couleur"], choix["type"]) != (f["couleur"], f["type"]):
            f["projet"] = origine
            f["couleur"], f["type"] = choix["couleur"], choix["type"]
            change = True
    if change:
        poser_prix(entree, bobines)
    return change


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
                for entree in nouvelles:
                    poser_prix(entree, bobines)
                    self.alerter(entree, bobines, reste)
                journal += nouvelles
                journal.sort(key=lambda e: e["date"])
                ecrire_json(self.donnees, "impressions.json", journal)
        return len(nouvelles)

    def alerter(self, entree, bobines, reste):
        """Prévient si l'impression demande plus de fil qu'il n'en reste, ou vide la dernière bobine."""
        lignes = []
        for f in entree["fils"]:
            cle = cle_fil(f)
            if cle not in bobines:
                continue
            avant = reste[cle]
            reste[cle] = avant - f["g"]
            nom = nom_couleur(f["couleur"])
            if f["g"] > avant:
                lignes.append("%s : il faut %s, il en reste environ %s sur la bobine."
                              % (nom, poids_texte(f["g"]), poids_texte(max(avant, 0))))
            elif avant >= PRESQUE_VIDE > reste[cle] and bobines[cle].get("reserve") == 0:
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
    return {"reseau": bool(r.get("reseau")), "code": str(r.get("code") or ""), "secret": str(r.get("secret") or "")}


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


def emplacements_connus(donnees):
    """Les emplacements de toutes les imprimantes, tels que gardés au dernier contact."""
    parc = lire(donnees, "imprimantes.json", {})
    plusieurs = len(parc) > 1
    liste = []
    for memoire in sorted(parc.values(), key=lambda m: m.get("numero", 0)):
        for cle, e in sorted((memoire.get("emplacements") or {}).items()):
            fiche = dict(e, nom=("%d·" % memoire.get("numero", 1) if plusieurs else "") + nom_emplacement(cle))
            poids_puce, remain = e.get("poids_puce") or 0, e.get("remain")
            lu = e.get("puce") and poids_puce > 0 and remain is not None and 0 <= remain <= 100
            fiche["reste_puce"] = round(poids_puce * remain / 100, 1) if lu else None
            liste.append(fiche)
    return liste


def etat_page(donnees, parc=None, local=True, port=PORT):
    """Tout ce que la page affiche, calculé à la demande."""
    journal, bobines = comptees(lire_journal(donnees)), lire_bobines(donnees)
    connues = lire(donnees, "couleurs.json", {})
    reste = restes(journal, bobines)
    en_cours = parc.en_cours() if parc else {}

    totaux, mois = {}, {}
    for entree in journal:
        p = part(entree)
        m = mois.setdefault(entree["date"][:7], {"impressions": 0, "g": 0.0, "cout": 0.0, "prix": False})
        m["impressions"] += 1
        prix = cout(entree)
        if prix is not None:
            m["cout"], m["prix"] = m["cout"] + prix, True
        for f in entree["fils"]:
            t = totaux.setdefault(cle_fil(f), {"noms": [], "g": 0.0, "m": 0.0, "n": 0})
            t["g"] += f["g"] * p
            t["m"] += f["m"] * p
            t["n"] += 1
            m["g"] += f["g"] * p
            if f.get("nom") and f["nom"] not in t["noms"]:
                t["noms"].append(f["nom"])

    charges, puces, codes = {}, {}, {}  # par couleur|type : emplacements, reste lu sur la puce, code de fil
    for e in emplacements_connus(donnees):
        if e.get("present") and e.get("couleur") and e.get("type"):
            cle = "%s|%s" % (e["couleur"], e["type"])
            charges.setdefault(cle, []).append(e["nom"])
            if e["reste_puce"] is not None:
                puces[cle] = (e["reste_puce"], e.get("poids_puce") or BOBINE_NEUVE)
            if e.get("indice"):
                codes[cle] = e["indice"]

    def fiche(cle):
        couleur, _, type_fil = cle.partition("|")
        return {"cle": cle, "couleur": couleur, "nom": nom_couleur(couleur), "type": type_fil,
                "emplacements": charges.get(cle, [])}

    def marque_probable(cle):
        if cle in puces:  # seules les bobines Bambu Lab portent une puce
            return "Bambu Lab"
        return CODES_MARQUE.get(codes.get(cle, "")) or marque_devinee(totaux[cle]["noms"] if cle in totaux
                                                                    else connues.get(cle, []))

    suivies, reste_total, a_racheter = [], 0.0, []
    for cle in sorted(set(bobines) | set(puces), key=cle_tri):
        b = bobines.get(cle)
        f = fiche(cle)
        if cle in puces:  # la puce d'une bobine Bambu Lab dit ce qu'il reste : elle fait foi
            valeur, plein = puces[cle]
            f["source"] = "puce"
        else:
            valeur, plein = reste[cle], b["poids"]
        reste_total += max(valeur, 0)
        # La jauge se lit par rapport à une bobine pleine, pas par rapport au poids saisi.
        f.update(suivie=b is not None, marque=(b or {}).get("marque") or marque_probable(cle),
                 poids=plein, depuis=(b or {}).get("depuis"), reste=round(valeur, 2),
                 part=round(max(min(valeur / max(plein, BOBINE_NEUVE), 1), 0), 4), bas=valeur < PRESQUE_VIDE,
                 prix=(b or {}).get("prix"), reserve=(b or {}).get("reserve"))
        if f["bas"] and f["reserve"] == 0:
            a_racheter.append(" ".join(x for x in (f["nom"], f["marque"], f["type"]) if x))
        suivies.append(f)

    # Fils chargés sur l'imprimante ou déjà imprimés, sans bobine suivie.
    a_suivre = []
    for cle in sorted((c for c in set(totaux) | set(charges) if c not in bobines and c not in puces), key=cle_tri):
        f = fiche(cle)
        f["marque"] = marque_probable(cle)
        a_suivre.append(f)

    autres = sorted((c for c in connues if c not in bobines and c not in totaux and c not in charges), key=cle_tri)
    maintenant = datetime.now()
    courant = mois.get(maintenant.strftime("%Y-%m"), {"impressions": 0, "g": 0.0, "cout": 0.0, "prix": False})
    total_cout = [m["cout"] for m in mois.values() if m["prix"]]

    imprimantes = parc.etats() if parc else []
    for i in imprimantes:
        i.update(court=ETATS_COURTS.get(i["etat"], ""), texte=ETATS_IMPRIMANTE.get(i["etat"], ""),
                 alerte=i["etat"] in ("bloquee", "refusee"))

    etat = {
        "imprimantes": imprimantes,
        "resume": {"impressions": len(journal), "depuis": journal[0]["date"] if journal else None,
                   "g": round(sum(t["g"] for t in totaux.values()), 2), "reste": round(reste_total, 2),
                   "cout": round(sum(total_cout), 2) if total_cout else None,
                   "mois": {"nom": MOIS[maintenant.month - 1], "impressions": courant["impressions"],
                            "g": round(courant["g"], 2), "cout": round(courant["cout"], 2) if courant["prix"] else None}},
        "bobines": suivies,
        "a_suivre": a_suivre,
        "a_racheter": a_racheter,
        "couleurs": [fiche(c) for c in [f["cle"] for f in a_suivre] + autres],
        "totaux": [dict(fiche(cle), fils=t["noms"], g=round(t["g"], 2), m=round(t["m"], 2), n=t["n"])
                   for cle, t in sorted(totaux.items(), key=lambda x: -x[1]["g"])],
        "mois": [{"cle": cle, "nom": "%s %s" % (MOIS[int(cle[5:7]) - 1], cle[:4]), "impressions": m["impressions"],
                  "g": round(m["g"], 2), "cout": round(m["cout"], 2) if m["prix"] else None}
                 for cle, m in sorted(mois.items(), reverse=True)],
        "impressions": [{"id": identifiant(en), "date": en["date"], "projet": en["projet"],
                         "secondes": en.get("secondes") or 0, "part": part(en), "cout": cout(en),
                         "en_cours": en_cours.get(en.get("tache")) if en.get("etat", "lancee") == "lancee" else None,
                         "fils": [{"couleur": f["couleur"], "g": round(f["g"] * part(en), 2)} for f in en["fils"]]}
                        for en in reversed(journal[-300:])],
        "alertes": list(reversed(lire_alertes(donnees))),
        "marques": marques_proposees(donnees),
        "types": TYPES,
        "local": local,
    }
    if local:  # les réglages ne se voient et ne se changent que depuis l'ordinateur lui-même
        reglages, adresse = lire_reglages(donnees), adresse_locale()
        etat["reglages"] = {"reseau": reglages["reseau"], "code": reglages["code"],
                            "adresse": "http://%s:%d/" % (adresse, port) if adresse else None}
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


class Serveur(ThreadingHTTPServer):
    # Sous Windows, cette option laisserait deux exemplaires écouter sur le même port.
    allow_reuse_address = not WINDOWS
    daemon_threads = True

    def server_bind(self):
        # HTTPServer cherche ici le nom de l'ordinateur sur le réseau, ce qui peut bloquer des dizaines
        # de secondes : ce nom est inutile.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


def lancer_serveur(donnees, port, parc=None, reseau=False):
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

        def fichier(self, chemin, type_mime, cache="no-store"):
            try:
                with open(chemin, "rb") as f:
                    self.repondre(200, f.read(), type_mime, cache)
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
                self.json(200, etat_page(donnees, parc, self.local(), port))
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
            if self.path == "/reglages":
                return self.regler(champs)
            if self.path == "/alerte/fermer":
                with verrou_alertes:
                    ecrire_json(donnees, "alertes.json", [a for a in lire_alertes(donnees)
                                                          if champs.get("id") not in (a.get("id"), "toutes")])
                return self.json(200, {"ok": True})
            if self.path in ("/impression", "/impression/supprimer"):
                return self.corriger(champs)
            bobine = bobine_demandee(champs)
            if not bobine or self.path not in ("/bobine", "/bobine/modifier", "/retirer"):
                return self.json(400, {"ok": False})
            cle = "%s|%s" % bobine
            bon_prix, prix = nombre_saisi(champs, "prix", 1000)
            bonne_reserve, reserve = nombre_saisi(champs, "reserve", 99, entier=True)
            bon_poids, grammes = nombre_saisi(champs, "poids", 20000)
            marque = " ".join(champs.get("marque", "").split())[:40]
            with verrou:
                bobines = lire_bobines(donnees)
                if self.path == "/retirer":
                    bobines.pop(cle, None)
                elif not bon_prix or not bonne_reserve:
                    return self.json(400, {"ok": False})
                elif self.path == "/bobine/modifier":
                    if cle not in bobines:
                        return self.json(400, {"ok": False})
                    bobines[cle].update(marque=marque, prix=prix or None, reserve=reserve)
                    # Le prix corrigé vaut pour toutes les impressions faites avec cette bobine.
                    journal = lire_journal(donnees)
                    for entree in journal:
                        if entree["date"] >= bobines[cle]["depuis"] and any(cle_fil(f) == cle for f in entree["fils"]):
                            poser_prix(entree, bobines)
                    ecrire_json(donnees, "impressions.json", journal)
                else:
                    if not bon_poids or not grammes:
                        return self.json(400, {"ok": False})
                    # Une bobine neuve repart de son poids : seules les impressions suivantes s'en déduisent.
                    bobines[cle] = {"couleur": bobine[0], "type": bobine[1], "marque": marque, "poids": grammes,
                                    "prix": prix or None, "reserve": reserve,
                                    "depuis": datetime.now().isoformat(timespec="seconds")}
                if marque and marque not in marques_proposees(donnees):  # une marque nouvelle est retenue
                    ecrire_json(donnees, "marques.json", lire(donnees, "marques.json", []) + [marque])
                ecrire_json(donnees, "bobines.json", bobines)
            self.json(200, {"ok": True})

        def corriger(self, champs):
            """Corrige la part imprimée d'une impression, ou la retire du compte."""
            bonne_part, pourcent = nombre_saisi(champs, "part", 100)
            with verrou:
                journal = lire_journal(donnees)
                entree = next((e for e in comptees(journal) if identifiant(e) == champs.get("id")), None)
                if not entree or (self.path == "/impression" and (not bonne_part or pourcent is None)):
                    return self.json(400, {"ok": False})
                if self.path == "/impression":
                    entree["part"], entree["corrigee"] = round(pourcent / 100, 4), True
                else:
                    entree["supprimee"] = True  # gardée, pour ne pas être recomptée si son fichier est relu
                ecrire_json(donnees, "impressions.json", journal)
            self.json(200, {"ok": True})

        def regler(self, champs):
            if not self.local():
                return self.repondre(403, b"refuse")
            with verrou:
                reglages = lire_reglages(donnees)
                reglages["reseau"] = champs.get("reseau") == "1"
                if reglages["reseau"] and not reglages["code"]:
                    reglages["code"] = "%06d" % secrets.randbelow(10 ** 6)
                    reglages["secret"] = secrets.token_hex(16)
                ecrire_json(donnees, "reglages.json", reglages)
            self.json(200, {"ok": True})

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


class Parc:
    """Les imprimantes connues de Bambu Studio : une écoute par imprimante."""

    def __init__(self, donnees, fenetres, conf=None, ac=None, adresses=None, delais=(STABILISATION, REMPLACEMENT)):
        self.donnees, self.fenetres, self.conf, self.ac = donnees, fenetres, conf, ac
        self.adresses = adresses or {}  # essais : {numéro de série ou "*": (hôte, port)}
        self.stabilisation, self.remplacement = delais
        self.imprimantes = {}  # numéro de série → Imprimante
        self.memoire = lire(donnees, "imprimantes.json", {})
        self.reprise = lire(donnees, "imprimante.json", None)  # mémoire d'avant, quand il n'y en avait qu'une
        self.ecriture = threading.Lock()

    def code(self, serie):
        return acces_imprimantes(self.conf or conf_bambu()).get(serie)

    def actualiser(self):
        """Lance l'écoute des imprimantes que Bambu Studio connaît et qu'on n'écoute pas encore."""
        for serie in acces_imprimantes(self.conf or conf_bambu()):
            if serie not in self.imprimantes:
                imprimante = Imprimante(self, len(self.imprimantes) + 1, serie)
                self.imprimantes[serie] = imprimante
                self.garder()
                imprimante.start()

    def liste(self):
        return sorted(self.imprimantes.values(), key=lambda i: i.numero)

    def garder(self):
        with self.ecriture:
            self.memoire = {i.repere: {"numero": i.numero, "ip": i.ip, "emplacements": i.emplacements}
                            for i in self.liste()}
            ecrire_json(self.donnees, "imprimantes.json", self.memoire)

    def etats(self):
        plusieurs = len(self.imprimantes) > 1
        return [{"nom": "Imprimante %d" % i.numero if plusieurs else "Imprimante", "etat": i.etat} for i in self.liste()]

    def en_cours(self):
        """{tâche: avancement en %} des impressions que les imprimantes sont en train de faire."""
        return {i.tache["ident"]: i.tache["percent"] for i in self.liste() if i.tache}


class Imprimante(threading.Thread):
    """Écoute l'état d'une imprimante sur le réseau local.

    Elle s'abonne à ses annonces et ne lui publie rien : aucune commande ne part. Elle en tire les
    bobines chargées ou retirées, l'impression en cours, son avancement et sa fin.
    """

    EN_COURS = ("PREPARE", "RUNNING", "PAUSE", "SLICING")

    def __init__(self, parc, numero, serie):
        super().__init__(daemon=True)
        self.parc, self.numero, self.serie = parc, numero, serie
        self.repere = hashlib.sha1(serie.encode("utf-8")).hexdigest()[:10]  # le numéro de série n'est pas gardé
        memoire = parc.memoire.get(self.repere) or (parc.reprise if numero == 1 else None) or {}
        self.ip = memoire.get("ip")
        self.emplacements = memoire.get("emplacements") or {}  # "0-1" → {present, couleur, type, …}
        self.attente = {}    # emplacement → {depart : présence avant le premier changement, dernier : heure}
        self.etat = "recherche"
        self.vue = {}        # ce que l'imprimante a annoncé de l'impression, messages partiels réunis
        self.tache = None    # l'impression en cours : {ident, percent, bacs, liee, debut}
        self.bac_actif = None

    def noter(self, etat, detail=""):
        if etat != self.etat:
            print("imprimante %d :" % self.numero, etat, detail, flush=True)
        self.etat = etat

    def designation(self, cle):
        plusieurs = len(self.parc.imprimantes) > 1
        return ("Imprimante %d, emplacement %s" if plusieurs else "%.0sEmplacement %s") % (self.numero, nom_emplacement(cle))

    def run(self):
        while True:
            pause = 30
            try:
                code = self.parc.code(self.serie)
                if not code:
                    self.noter("sans_code")
                    pause = 300
                else:
                    self.joindre(code)
            except Refus as e:
                self.noter("refusee", str(e))
                pause = 300
            fin = time.time() + pause
            while time.time() < fin:  # les changements en attente s'annoncent même liaison coupée
                self.verifier()
                time.sleep(2)

    def joindre(self, code):
        """Essaie les adresses connues ; ne rend la main que lorsque la liaison tombe."""
        imposee = self.parc.adresses.get(self.serie) or self.parc.adresses.get("*")
        if imposee:
            candidats = [imposee]
        else:
            candidats = [(h, 8883) for h in dict.fromkeys(([self.ip] if self.ip else []) + adresses_vues())]
        if not candidats:
            self.noter("injoignable", "adresse inconnue : ouvrir Bambu Studio une fois")
        for hote, port in candidats:
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
        contexte = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        contexte.check_hostname = False  # le certificat porte le numéro de série, pas l'adresse
        if self.parc.ac:
            contexte.load_verify_locations(self.parc.ac)
        else:
            contexte.load_verify_locations(cadata=AC_BAMBU)
            for certificat in certificats_installes():
                try:
                    contexte.load_verify_locations(certificat)
                except (OSError, ssl.SSLError):
                    pass
        # Une imprimante occupée met parfois plus de dix secondes à établir la liaison chiffrée.
        with contexte.wrap_socket(socket.create_connection((hote, port), timeout=25)) as liaison:
            sujet = dict(x[0] for x in liaison.getpeercert().get("subject", ()))
            if sujet.get("commonName") != self.serie:
                raise AutreAppareil()
            liaison.sendall(paquet_mqtt(0x10, chaine_mqtt("MQTT") + bytes([4, 0xC2]) + struct.pack("!H", 60)
                                        + chaine_mqtt("blokbambu-%d-%d" % (os.getpid(), self.numero))
                                        + chaine_mqtt("bblp") + chaine_mqtt(code)))
            flux = Flux(liaison)
            reponse = flux.paquet()
            if not reponse or reponse[0] != 0x20 or len(reponse[1]) < 2:
                raise ConnectionError("pas de réponse de l'imprimante")
            if reponse[1][1] != 0:
                raise Refus("code d'accès refusé")
            if hote != self.ip:
                self.ip = hote
                self.parc.garder()
            liaison.sendall(paquet_mqtt(0x82, struct.pack("!H", 1) + chaine_mqtt("device/%s/report" % self.serie) + b"\x00"))
            liaison.settimeout(5)
            entendu = ping = time.time()
            while True:
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
        memoire = json.dumps(self.emplacements, sort_keys=True)
        if isinstance(p.get("vt_tray"), dict):
            self.lire_bac(EXTERNE, p["vt_tray"])
        if isinstance(p.get("ams"), dict):
            self.lire_ams(p["ams"])
        if json.dumps(self.emplacements, sort_keys=True) != memoire:
            self.parc.garder()
        try:
            self.suivre_impression(p)
        except (KeyError, TypeError, ValueError) as e:
            print("impression mal comprise :", repr(e), flush=True)

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
                print("emplacement", nom_emplacement(cle), ":", "chargé" if e["present"] else "retiré", flush=True)
        if "tray_now" in ams:  # l'emplacement qui alimente la buse : 255 = aucun, 254 = bobine externe
            try:
                n = int(ams["tray_now"])
            except (TypeError, ValueError):
                n = 255
            self.bac_actif = EXTERNE if n == 254 else "%d-%d" % (n // 4, n % 4) if 0 <= n < 16 else None
            self.noter_bac()

    # ----- l'impression en cours

    def suivre_impression(self, p):
        premier = "gcode_state" not in self.vue
        for champ in ("gcode_state", "mc_percent", "subtask_id", "task_id", "subtask_name", "gcode_file"):
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
        if etat in self.EN_COURS:
            if not self.tache or self.tache["ident"] != ident:
                # `direct` : on a vu l'impression démarrer, et non découvert une impression déjà lancée
                self.tache = {"ident": ident, "percent": percent, "bacs": [], "liee": False, "debut": time.time(),
                              "direct": not premier}
                self.lier()
            self.tache["percent"] = percent
            if etat == "RUNNING":
                self.noter_bac()
        elif etat in ("FINISH", "FAILED"):
            tache, self.tache = (self.tache if self.tache and self.tache["ident"] == ident else None), None
            self.conclure(ident, etat, tache["percent"] if tache else percent, tache["bacs"] if tache else [])
        else:
            self.tache = None

    def fils_utilises(self, bacs):
        return [{"couleur": e["couleur"], "type": e["type"]} for e in (self.emplacements.get(b, {}) for b in bacs)
                if e.get("couleur") and e.get("type")]

    def noter_bac(self):
        """Retient l'emplacement qui alimente l'impression en cours ; un seul fil : on sait déjà d'où il vient."""
        t = self.tache
        if not t or self.vue.get("gcode_state") != "RUNNING" or not self.bac_actif or self.bac_actif in t["bacs"]:
            return
        t["bacs"].append(self.bac_actif)
        self.reporter(t["ident"], lambda entree, bobines: attribuer_bacs(entree, self.fils_utilises(t["bacs"]), bobines, False))

    def reporter(self, ident, changement):
        """Applique un changement à l'impression du journal liée à cette tâche, s'il y en a une."""
        with verrou:
            journal = lire_journal(self.parc.donnees)
            entree = next((e for e in journal if e.get("tache") == ident and not e.get("supprimee")), None)
            if entree and changement(entree, lire_bobines(self.parc.donnees)):
                ecrire_json(self.parc.donnees, "impressions.json", journal)

    def lier(self):
        """Rattache l'impression que fait l'imprimante à celle que Bambu Studio vient de lancer d'ici."""
        t = self.tache
        nom = str(self.vue.get("subtask_name") or "").lower()
        with verrou:
            journal = lire_journal(self.parc.donnees)
            if any(e.get("tache") == t["ident"] for e in journal):
                t["liee"] = True
                return
            maintenant = datetime.now()
            limite = (maintenant - timedelta(hours=48)).isoformat(timespec="seconds")
            libres = [e for e in comptees(journal)
                      if e.get("etat", "lancee") == "lancee" and not e.get("tache") and e["date"] >= limite]
            # Bambu Studio nomme l'impression d'après le projet (« projet_plate_1 ») : c'est le lien le plus sûr.
            choisies = [e for e in libres if nom and nom.startswith(e["projet"].lower())]
            if not choisies and t["direct"]:  # nom changé à l'envoi : la dernière lancée dans les vingt minutes
                recent = (maintenant - timedelta(minutes=20)).isoformat(timespec="seconds")
                choisies = [e for e in libres if e["date"] >= recent]
            if choisies:
                choisies[-1]["tache"], choisies[-1]["imprimante"] = t["ident"], self.numero
                ecrire_json(self.parc.donnees, "impressions.json", journal)
                t["liee"] = True
                print("impression suivie :", choisies[-1]["projet"], flush=True)

    def conclure(self, ident, etat, percent, bacs):
        """L'impression est finie ou arrêtée : une impression arrêtée ne compte que pour la part faite."""
        def changement(entree, bobines):
            if entree.get("etat", "lancee") != "lancee":
                return False
            entree["etat"] = "terminee" if etat == "FINISH" else "arretee"
            if etat == "FAILED" and not entree.get("corrigee"):
                entree["part"] = round(percent / 100, 2)
            attribuer_bacs(entree, self.fils_utilises(bacs), bobines, True)
            print("impression", "terminée :" if etat == "FINISH" else "arrêtée à %d %% :" % percent, entree["projet"], flush=True)
            return True
        self.reporter(ident, changement)

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
        t = self.tache
        if t and not t["liee"] and maintenant - t["debut"] < 180 and maintenant - t.get("essai", 0) > 5:
            t["essai"] = maintenant
            self.lier()


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
    serveur, ouvert, essai, vu = None, None, 0, 0
    while True:
        maintenant = time.time()
        if parc and maintenant - vu > 60:  # une imprimante ajoutée dans Bambu Studio est écoutée dans la minute
            vu = maintenant
            parc.actualiser()
        reseau = lire_reglages(o.donnees)["reseau"]
        if serveur is not None and ouvert != reseau:  # le réglage a changé : la page change de porte
            serveur.shutdown()
            serveur.server_close()
            serveur = None
        if serveur is None and maintenant >= essai:
            try:
                serveur, ouvert = lancer_serveur(o.donnees, o.port, parc, reseau), reseau
                print("page sur http://127.0.0.1:%d/%s" % (o.port, ", ouverte au réseau local" if reseau else ""), flush=True)
            except OSError as e:  # port occupé : la surveillance continue, on réessaie plus tard
                print("page indisponible :", e, file=sys.stderr, flush=True)
                essai = maintenant + 30
        try:
            veilleur.passage()
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
