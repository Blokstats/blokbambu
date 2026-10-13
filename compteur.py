#!/usr/bin/python3
"""BlokFil — compte le fil utilisé par couleur à chaque impression lancée depuis Bambu Studio.

Quand on clique sur « Imprimer », Bambu Studio écrit le fichier envoyé à l'imprimante dans
son dossier de travail temporaire : <tmp>/bamboo_model/<jour>/<session>/Metadata/.<pid>.<n>.3mf
Ce fichier contient Metadata/slice_info.config, avec les grammes et les mètres par fil.
Le compteur surveille ce dossier, note chaque envoi dans un journal et tient la page à jour.
La page est servie sur ce Mac seulement (127.0.0.1) : c'est là qu'on suit et qu'on remet à
neuf les bobines.
Il écoute aussi l'imprimante sur le réseau local, sans rien lui envoyer, pour prévenir par
une fenêtre quand une bobine est chargée ou retirée.
"""
import argparse
import glob
import html
import ipaddress
import json
import os
import re
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

DONNEES = os.path.expanduser("~/Library/Application Support/BlokFil")
PAGE = "FIL PAR COULEUR.html"
PORT = 47815
PAUSE = 3  # secondes entre deux passages
BOBINE_NEUVE = 1000  # grammes
PRESQUE_VIDE = 100   # grammes : en dessous, la bobine est signalée
TYPES = ["PLA", "PETG", "ABS", "ASA", "TPU", "PA", "PC"]

# L'imprimante : Bambu Studio garde son numéro de série et son code d'accès, et livre le
# certificat qui permet de la reconnaître avant de lui présenter ce code.
BAMBU_CONF = os.path.expanduser("~/Library/Application Support/BambuStudio/BambuStudio.conf")
CERTIFICAT = "BambuStudio.app/Contents/Resources/cert/printer.cer"
BAMBU_AC = next((c for c in ("/Applications/" + CERTIFICAT, os.path.expanduser("~/Applications/" + CERTIFICAT))
                 if os.path.exists(c)), "/Applications/" + CERTIFICAT)
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

MOIS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]

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


def dossier_temporaire():
    try:
        return subprocess.check_output(["getconf", "DARWIN_USER_TEMP_DIR"], text=True).strip()
    except Exception:
        return tempfile.gettempdir()


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
        origine = open(os.path.join(session, "origin.txt"), encoding="utf-8").read().strip()
    except OSError:
        origine = ""
    if origine:
        return os.path.splitext(os.path.basename(origine))[0], origine
    return (objets[0] if objets else "Projet sans nom"), ""


# ---------------------------------------------------------------- fichiers de données

def lire(donnees, nom, defaut):
    try:
        return json.load(open(os.path.join(donnees, nom), encoding="utf-8"))
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


# ---------------------------------------------------------------- surveillance

class Veilleur:
    def __init__(self, racine, donnees, port):
        self.racine, self.donnees, self.port = racine, donnees, port
        self.observes = {}  # chemin → (date de modification, taille) au passage précédent
        self.rejetes = {}   # chemin → même repère, pour un fichier illisible

    def passage(self):
        """Note les nouveaux envois. Rend le nombre d'impressions ajoutées."""
        motif = os.path.join(self.racine, "bamboo_model", "*", "*", "Metadata", ".*.3mf")
        nouvelles = []
        for chemin in glob.glob(motif):
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
            self.rejetes[chemin] = repere
            cle = "%s|%d|%d" % (chemin, s.st_mtime_ns, s.st_size)
            if any(e.get("cle") == cle for e in lire_journal(self.donnees)):
                continue
            try:
                envoi = lire_envoi(chemin)
            except (zipfile.BadZipFile, ET.ParseError, OSError, KeyError, ValueError) as e:
                print("illisible :", chemin, e, flush=True)
                envoi = None
            if not envoi:
                continue
            session = os.path.dirname(os.path.dirname(chemin))
            projet, fichier = nom_projet(session, envoi.pop("objets"))
            entree = {"cle": cle, "date": datetime.fromtimestamp(s.st_mtime).isoformat(timespec="seconds"),
                      "projet": projet, "fichier": fichier}
            entree.update(envoi)
            nouvelles.append(entree)
            print("impression comptée :", projet, envoi["fils"], flush=True)
        if nouvelles:
            with verrou:
                journal = lire_journal(self.donnees) + nouvelles
                journal.sort(key=lambda e: e["date"])
                ecrire_json(self.donnees, "impressions.json", journal)
            ecrire_page(self.donnees, self.port)
        return len(nouvelles)


# ---------------------------------------------------------------- page

def nombre(x, decimales=1):
    return ("%.*f" % (decimales, x)).replace(".", ",")


def poids(g):
    if g >= 1000:
        return ("%.2f" % (g / 1000)).rstrip("0").rstrip(".").replace(".", ",") + " kg"
    texte = nombre(g, 1 if g < 100 else 0)
    return (texte[:-2] if texte.endswith(",0") else texte) + " g"


def longueur(m):
    return nombre(m, 1 if m < 100 else 0) + " m"


def duree(secondes):
    h, m = secondes // 3600, (secondes % 3600) // 60
    return "%d h %02d" % (h, m) if h else "%d min" % max(m, 1)


def jour(d, annee=True):
    return "%d %s%s" % (d.day, MOIS[d.month - 1], " %d" % d.year if annee else "")


def pastille(code):
    return '<span class="pastille" style="background:%s"></span>' % html.escape(code)


def cle_tri(cle):
    couleur, _, type_fil = cle.partition("|")
    return (nom_couleur(couleur), couleur, type_fil)


STYLE = """
:root { --fond:#f6f5f2; --carte:#fff; --texte:#161616; --discret:#6f6c66; --trait:#e4e1da; --piste:#eeebe5; --alerte:#b3261e; }
@media (prefers-color-scheme: dark) {
  :root { --fond:#141413; --carte:#1d1d1b; --texte:#f2f0eb; --discret:#9b978f; --trait:#2e2d2a; --piste:#262523; --alerte:#f2877f; }
}
* { box-sizing:border-box; }
body { margin:0; background:var(--fond); color:var(--texte);
  font:16px/1.45 -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif; -webkit-font-smoothing:antialiased; }
main { max-width:760px; margin:0 auto; padding:48px 16px 64px; }
h1 { font-size:32px; line-height:1.1; margin:0 0 8px; letter-spacing:-.02em; }
h2 { font-size:13px; text-transform:uppercase; letter-spacing:.08em; color:var(--discret); margin:40px 0 12px; font-weight:600; }
small { display:block; color:var(--discret); font-size:13px; }
.resume { color:var(--discret); margin:0; }
table { width:100%; border-collapse:collapse; background:var(--carte); border:1px solid var(--trait); border-radius:14px; overflow:hidden; }
th { font-size:12px; font-weight:600; color:var(--discret); text-align:left; padding:12px 16px; border-bottom:1px solid var(--trait); }
td { padding:14px 16px; border-bottom:1px solid var(--trait); vertical-align:top; }
tr:last-child td { border-bottom:0; }
.n { text-align:right; white-space:nowrap; font-variant-numeric:tabular-nums; }
.fort { font-weight:600; }
.couleur { display:flex; gap:12px; align-items:center; min-width:0; }
code { font:12px ui-monospace, SFMono-Regular, Menlo, monospace; color:var(--discret); margin-left:4px; }
.pastille { flex:none; display:inline-block; width:22px; height:22px; border-radius:50%; border:1px solid var(--trait);
  box-shadow:inset 0 0 0 1px rgba(128,128,128,.25); }
.barre { height:6px; border-radius:3px; background:var(--piste); margin-top:10px; overflow:hidden; }
.barre i { display:block; height:100%; border-radius:3px; box-shadow:inset 0 0 0 1px rgba(128,128,128,.35); }
.jauge { height:8px; border-radius:4px; background:var(--piste); margin-top:12px; overflow:hidden; }
.jauge i { display:block; height:100%; border-radius:4px; background:var(--texte); }
.bas .jauge i { background:var(--alerte); }
.liste { list-style:none; margin:0; padding:0; background:var(--carte); border:1px solid var(--trait); border-radius:14px; }
.liste > li { padding:14px 16px; border-bottom:1px solid var(--trait); }
.liste > li:last-child { border-bottom:0; }
.ligne { display:flex; gap:16px; justify-content:space-between; align-items:center; }
.reste { text-align:right; white-space:nowrap; font-variant-numeric:tabular-nums; }
.reste strong { font-size:18px; }
.bas .reste strong, .bas .etat { color:var(--alerte); }
.etat { display:inline; }
.sans { color:var(--discret); }
.fils { display:flex; flex-wrap:wrap; gap:6px 14px; justify-content:flex-end; }
.fil { display:inline-flex; align-items:center; gap:6px; white-space:nowrap; font-variant-numeric:tabular-nums; }
.fil .pastille { width:14px; height:14px; }
.vide { background:var(--carte); border:1px solid var(--trait); border-radius:14px; padding:28px 20px; color:var(--discret); margin:0; }
details { margin-top:10px; }
summary { cursor:pointer; color:var(--discret); font-size:14px; width:fit-content; }
summary:hover { color:var(--texte); }
form { display:flex; flex-wrap:wrap; gap:10px 14px; align-items:center; margin:12px 0 0; }
label { display:inline-flex; align-items:center; gap:8px; font-size:14px; max-width:100%; }
label select { min-width:0; flex:1 1 auto; }
input, select, button { font:inherit; font-size:15px; color:var(--texte); background:var(--fond); border:1px solid var(--trait);
  border-radius:9px; padding:7px 10px; min-height:38px; }
input[type=number] { width:92px; text-align:right; }
input[type=color] { width:46px; padding:3px; }
select { max-width:100%; }
button { cursor:pointer; background:var(--texte); color:var(--fond); border-color:var(--texte); font-weight:600; padding:7px 14px; }
button.discret { background:none; color:var(--discret); border-color:var(--trait); font-weight:400; }
.ajout { margin:12px 0 0; padding:14px 16px; background:var(--carte); border:1px solid var(--trait); border-radius:14px; }
.ajout .autre { display:none; gap:10px; align-items:center; }
.imprimante { margin:12px 2px 0; }
.ajout:has(select[name=connue] option[value=""]:checked) .autre, .ajout.libre .autre { display:inline-flex; }
a { color:inherit; }
footer { color:var(--discret); font-size:13px; margin-top:28px; }
@media (max-width:560px) {
  th:nth-child(3), td:nth-child(3) { display:none; }
  .impressions .ligne { flex-direction:column; align-items:flex-start; gap:10px; }
  .fils { justify-content:flex-start; }
}
"""


def rendre(donnees, port, vivant, imprimante=None):
    """La page. `vivant` : servie par le compteur, avec les boutons ; sinon simple copie à lire."""
    e = html.escape
    journal, bobines = lire_journal(donnees), lire_bobines(donnees)
    connues = lire(donnees, "couleurs.json", {})

    totaux = {}
    for entree in journal:
        for f in entree["fils"]:
            cle = "%s|%s" % (f["couleur"], f["type"])
            t = totaux.setdefault(cle, {"couleur": f["couleur"], "type": f["type"], "noms": [], "g": 0.0, "m": 0.0, "n": 0})
            t["g"] += f["g"]
            t["m"] += f["m"]
            t["n"] += 1
            if f.get("nom") and f["nom"] not in t["noms"]:
                t["noms"].append(f["nom"])

    charges = {}  # couleur|type → emplacements de l'imprimante où ce fil est chargé
    for c, emplacement in sorted((lire(donnees, "imprimante.json", {}).get("emplacements") or {}).items()):
        if emplacement.get("present") and emplacement.get("couleur") and emplacement.get("type"):
            charges.setdefault("%s|%s" % (emplacement["couleur"], emplacement["type"]), []).append(nom_emplacement(c))

    def detail(cle, type_fil, liste=False):
        noms = connues.get(cle, []) if liste else (totaux[cle]["noms"] if cle in totaux else [])
        place = ["emplacement " + ", ".join(charges[cle])] if cle in charges else []
        return " · ".join(([type_fil] if type_fil else []) + noms + place)

    def champs_caches(couleur, type_fil):
        return '<input type="hidden" name="couleur" value="%s"><input type="hidden" name="type" value="%s">' % (
            e(couleur), e(type_fil))

    def champ_poids(valeur):
        return ('<label>Fil sur la bobine <input type="number" name="poids" value="%d" min="1" max="20000" '
                'step="1" required> g</label>' % valeur)

    corps = []

    # --- bobines
    corps.append("<h2>Bobines</h2>")
    lignes = []
    for cle in sorted(bobines, key=cle_tri):
        b = bobines[cle]
        utilise = sum(f["g"] for en in journal if en["date"] >= b["depuis"]
                      for f in en["fils"] if "%s|%s" % (f["couleur"], f["type"]) == cle)
        reste = b["poids"] - utilise
        part = max(min(reste / max(b["poids"], BOBINE_NEUVE), 1), 0)
        bas = reste < PRESQUE_VIDE
        etat = ' · <span class="etat">%s</span>' % ("vide" if reste <= 0 else "bientôt vide") if bas else ""
        actions = ""
        if vivant:
            actions = ('<details><summary>Nouvelle bobine</summary>'
                       '<form method="post" action="/bobine">%s%s<button>Valider</button></form>'
                       '<form method="post" action="/retirer">%s<button class="discret">Ne plus suivre cette bobine</button></form>'
                       '</details>') % (champs_caches(b["couleur"], b["type"]), champ_poids(BOBINE_NEUVE),
                                        champs_caches(b["couleur"], b["type"]))
        lignes.append(
            '<li class="%s"><div class="ligne"><div class="couleur">%s<div><strong>%s</strong> <code>%s</code>'
            '<small>%s · posée le %s%s</small></div></div>'
            '<div class="reste"><strong>%s</strong><small>restants</small></div></div>'
            '<div class="jauge"><i style="width:%.1f%%"></i></div>%s</li>' % (
                "bas" if bas else "", pastille(b["couleur"]), e(nom_couleur(b["couleur"])), e(b["couleur"]),
                e(detail(cle, b["type"])), jour(datetime.fromisoformat(b["depuis"]), False), etat,
                poids(max(reste, 0)), 100 * part, actions))
    # fils chargés sur l'imprimante ou déjà imprimés, sans bobine suivie
    a_suivre = sorted((c for c in set(totaux) | set(charges) if c not in bobines), key=cle_tri)
    for cle in a_suivre:
        t = {"couleur": cle.partition("|")[0], "type": cle.partition("|")[2]}
        actions = ""
        if vivant:
            actions = ('<details><summary>Suivre cette bobine</summary><form method="post" action="/bobine">%s%s'
                       '<button>Valider</button></form></details>') % (
                           champs_caches(t["couleur"], t["type"]), champ_poids(BOBINE_NEUVE))
        lignes.append(
            '<li><div class="ligne"><div class="couleur">%s<div><strong>%s</strong> <code>%s</code>'
            '<small>%s</small></div></div><div class="reste sans">pas de bobine suivie</div></div>%s</li>' % (
                pastille(t["couleur"]), e(nom_couleur(t["couleur"])), e(t["couleur"]), e(detail(cle, t["type"])), actions))
    if lignes:
        corps.append('<ul class="liste">%s</ul>' % "".join(lignes))
    else:
        corps.append('<p class="vide">Aucune bobine suivie. Ajoutez celles qui sont sur l\'imprimante : '
                     "chaque impression s'en déduira.</p>")

    if vivant:
        proposees = sorted((c for c in connues if c not in bobines and c not in a_suivre), key=cle_tri)
        options = "".join('<option value="%s">%s %s%s</option>' % (
            e(c), e(nom_couleur(c.partition("|")[0])), e(c.partition("|")[0]),
            e(" · " + detail(c, c.partition("|")[2], True))) for c in proposees)
        liste = ('<label>Couleur <select name="connue">%s<option value="">Autre couleur…</option></select></label>'
                 % options) if proposees else ""
        types = "".join("<option>%s</option>" % t for t in TYPES)
        corps.append(
            '<form method="post" action="/bobine" class="ajout%s">%s'
            '<span class="autre"><input type="color" name="couleur" value="#161616" aria-label="Couleur du fil">'
            '<select name="type" aria-label="Type de fil">%s</select></span>%s<button>Ajouter la bobine</button></form>' % (
                "" if proposees else " libre", liste, types, champ_poids(BOBINE_NEUVE)))
        if imprimante in ETATS_IMPRIMANTE:
            corps.append('<p class="imprimante"><small>%s</small></p>' % e(ETATS_IMPRIMANTE[imprimante]))
    else:
        corps.append('<p><small>Pour ajouter une bobine ou en remettre une à neuf : '
                     '<a href="http://127.0.0.1:%d/">ouvrir la page du compteur</a>.</small></p>' % port)

    # --- fil utilisé
    corps.append("<h2>Fil utilisé</h2>")
    lignes = sorted(totaux.values(), key=lambda t: -t["g"])
    if lignes:
        maxi = max(t["g"] for t in lignes)
        corps.append('<table><thead><tr><th>Couleur</th><th class="n">Poids</th>'
                     '<th class="n">Longueur</th><th class="n">Impressions</th></tr></thead><tbody>')
        for t in lignes:
            corps.append(
                '<tr><td><div class="couleur">%s<div><strong>%s</strong> <code>%s</code>'
                '<small>%s</small></div></div><div class="barre"><i style="width:%.1f%%;background:%s"></i></div></td>'
                '<td class="n fort">%s</td><td class="n">%s</td><td class="n">%d</td></tr>' % (
                    pastille(t["couleur"]), e(nom_couleur(t["couleur"])), e(t["couleur"]),
                    e(detail("%s|%s" % (t["couleur"], t["type"]), t["type"])),
                    max(100 * t["g"] / maxi, 0.6), e(t["couleur"]), poids(t["g"]), longueur(t["m"]), t["n"]))
        corps.append("</tbody></table>")
        corps.append('<h2>Impressions</h2><ul class="liste impressions">')
        for entree in reversed(journal):
            d = datetime.fromisoformat(entree["date"])
            pl = entree.get("plateaux") or []
            plateau = ("plateau %d" % pl[0]) if len(pl) == 1 else ("plateaux %s" % ", ".join(map(str, pl)))
            fils = "".join('<span class="fil">%s%s</span>' % (pastille(f["couleur"]), poids(f["g"]))
                           for f in entree["fils"])
            corps.append('<li><div class="ligne"><div><strong>%s</strong><small>%s à %02d h %02d · %s · %s</small></div>'
                         '<div class="fils">%s</div></div></li>' % (
                             e(entree["projet"]), jour(d), d.hour, d.minute, plateau, duree(entree["secondes"]), fils))
        corps.append("</ul>")
    else:
        corps.append('<p class="vide">Lancez une impression depuis Bambu Studio sur ce Mac : elle apparaîtra ici, '
                     "avec le fil utilisé par couleur.</p>")

    if journal:
        n = len(journal)
        resume = "%d impression%s lancée%s depuis le %s · %s de fil" % (
            n, "s" if n > 1 else "", "s" if n > 1 else "", jour(datetime.fromisoformat(journal[0]["date"])),
            poids(sum(t["g"] for t in lignes)))
    else:
        resume = "Aucune impression comptée pour l'instant."

    maintenant = datetime.now()
    # La copie posée dans le dossier BAMBU passe d'elle-même à la page du compteur, qui a les boutons.
    # Ce script vient du compteur : s'il est arrêté, rien ne se charge et la copie reste affichée.
    saut = "" if vivant else '<script src="http://127.0.0.1:%d/saut.js"></script>\n' % port
    return "".join([
        '<!doctype html>\n<html lang="fr"><head><meta charset="utf-8">\n',
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n',
        '<meta name="color-scheme" content="light dark">\n<title>Fil par couleur</title>\n',
        "<style>", STYLE, "</style></head><body><main>\n",
        "<h1>Fil par couleur</h1>\n", '<p class="resume">%s</p>\n' % e(resume), "\n".join(corps),
        "\n<footer>Poids estimés par Bambu Studio au lancement de chaque impression, purge comprise. ",
        "Mis à jour le %s à %02d h %02d.</footer>\n" % (jour(maintenant), maintenant.hour, maintenant.minute),
        "</main>", saut, "</body></html>\n"])


def ecrire_page(donnees, port):
    ecrire(os.path.join(donnees, PAGE), rendre(donnees, port, vivant=False))


# ---------------------------------------------------------------- serveur de la page

def bobine_demandee(champs):
    """(couleur, type) d'un formulaire, ou None si la saisie n'est pas valable."""
    if champs.get("connue"):
        couleur, _, type_fil = champs["connue"].partition("|")
    else:
        couleur, type_fil = champs.get("couleur", ""), champs.get("type", "")
    couleur = couleur.strip().upper()
    if not re.fullmatch(r"#[0-9A-F]{6}", couleur) or not re.fullmatch(r"[A-Za-z0-9+\- ]{1,16}", type_fil):
        return None
    return couleur, type_fil


def lancer_serveur(donnees, port, imprimante=None):
    adresses = ("127.0.0.1:%d" % port, "localhost:%d" % port)

    class Page(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def autorise(self):
            # Seul un navigateur sur ce Mac, depuis la page elle-même, peut modifier les bobines.
            if self.headers.get("Host") not in adresses:
                return False
            if self.command == "POST":
                return self.headers.get("Origin") in ["http://" + a for a in adresses]
            return True

        def repondre(self, code, corps, type_mime="text/html; charset=utf-8", entetes=()):
            self.send_response(code)
            self.send_header("Content-Type", type_mime)
            self.send_header("Content-Length", str(len(corps)))
            self.send_header("Cache-Control", "no-store")
            for nom, valeur in entetes:
                self.send_header(nom, valeur)
            self.end_headers()
            self.wfile.write(corps)

        def do_GET(self):
            chemin = self.path.split("?")[0]
            if not self.autorise():
                self.repondre(403, b"refuse", "text/plain")
            elif chemin == "/saut.js":
                self.repondre(200, b'location.replace("http://127.0.0.1:%d/");' % port, "text/javascript")
            elif chemin == "/":
                etat = imprimante.etat if imprimante else None
                self.repondre(200, rendre(donnees, port, vivant=True, imprimante=etat).encode("utf-8"))
            else:
                self.repondre(404, b"introuvable", "text/plain")

        def do_POST(self):
            taille = int(self.headers.get("Content-Length") or 0)
            if not self.autorise() or taille > 4096:
                return self.repondre(403, b"refuse", "text/plain")
            champs = {k: v[0] for k, v in parse_qs(self.rfile.read(taille).decode("utf-8", "replace")).items()}
            bobine = bobine_demandee(champs)
            if bobine and self.path in ("/bobine", "/retirer"):
                cle = "%s|%s" % bobine
                with verrou:
                    bobines = lire_bobines(donnees)
                    if self.path == "/retirer":
                        bobines.pop(cle, None)
                    else:
                        try:
                            grammes = float(champs.get("poids", "").replace(",", "."))
                        except ValueError:
                            grammes = 0
                        if 0 < grammes <= 20000:
                            # Une bobine neuve repart de son poids : seules les impressions suivantes s'en déduisent.
                            bobines[cle] = {"couleur": bobine[0], "type": bobine[1], "poids": grammes,
                                            "depuis": datetime.now().isoformat(timespec="seconds")}
                    ecrire_json(donnees, "bobines.json", bobines)
                ecrire_page(donnees, port)
            self.repondre(303, b"", "text/plain", [("Location", "/")])

    serveur = ThreadingHTTPServer(("127.0.0.1", port), Page)
    threading.Thread(target=serveur.serve_forever, daemon=True).start()
    return serveur


# ---------------------------------------------------------------- imprimante

class Refus(Exception):
    pass


def acces_imprimante(conf):
    """(numéro de série, code d'accès) gardés par Bambu Studio, ou None. Le code n'est écrit nulle part."""
    try:
        d = json.load(open(conf, encoding="utf-8"))
    except (OSError, ValueError):
        return None
    choisie = (d.get("app") or {}).get("user_last_selected_machine")
    listes = [d.get(c) or {} for c in ("user_access_code", "access_code")]
    for codes in listes:
        if choisie and codes.get(choisie):
            return choisie, codes[choisie]
    for codes in listes:
        for serie, code in codes.items():
            if code:
                return serie, code
    return None


def adresse_vue_par_bambu():
    """Adresse locale de l'imprimante, lue dans la connexion que Bambu Studio a déjà ouverte vers elle."""
    try:
        sortie = subprocess.run(["/usr/sbin/lsof", "-nP", "-a", "-c", "BambuStu", "-iTCP:8883"],
                                capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for m in re.finditer(r"->(\d+\.\d+\.\d+\.\d+):8883", sortie):
        if ipaddress.ip_address(m.group(1)).is_private:
            return m.group(1)
    return None


def nom_emplacement(cle):
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


class Imprimante(threading.Thread):
    """Écoute l'état de l'imprimante sur le réseau local et prévient quand une bobine change.

    Il s'abonne aux annonces de l'imprimante et ne lui publie rien : aucune commande ne part.
    """

    def __init__(self, donnees, port, conf=BAMBU_CONF, ac=BAMBU_AC, adresse=None, fenetre_essai=None,
                 delais=(STABILISATION, REMPLACEMENT)):
        super().__init__(daemon=True)
        self.donnees, self.port, self.conf, self.ac = donnees, port, conf, ac
        self.adresse, self.fenetre_essai = adresse, fenetre_essai
        self.stabilisation, self.remplacement = delais
        memoire = lire(donnees, "imprimante.json", {})
        self.ip = memoire.get("ip")
        self.emplacements = memoire.get("emplacements") or {}  # "0-1" → {present, couleur, type}
        self.attente = {}  # emplacement → {depart : présence avant le premier changement, dernier : heure}
        self.etat = "recherche"
        self.fenetres = threading.Lock()

    def noter(self, etat, detail=""):
        if etat != self.etat:
            print("imprimante :", etat, detail, flush=True)
        self.etat = etat

    def run(self):
        while True:
            pause = 30
            try:
                acces = acces_imprimante(self.conf)
                if not acces:
                    self.noter("sans_code")
                    pause = 300
                else:
                    hote, port = self.adresse or (adresse_vue_par_bambu() or self.ip, 8883)
                    if hote:
                        self.ecouter(hote, port, *acces)
                    else:
                        self.noter("injoignable", "adresse inconnue : ouvrir Bambu Studio une fois")
            except Refus as e:
                self.noter("refusee", str(e))
                pause = 300
            except (OSError, ssl.SSLError, ValueError, struct.error) as e:
                # « No route to host » alors que le Mac est sur le réseau : refus de macOS (Réseau local).
                self.noter("bloquee" if getattr(e, "errno", None) == 65 else "injoignable", str(e))
            fin = time.time() + pause
            while time.time() < fin:  # les changements en attente s'annoncent même liaison coupée
                self.verifier()
                time.sleep(2)

    def ecouter(self, hote, port, serie, code):
        contexte = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        contexte.check_hostname = False  # le certificat porte le numéro de série, pas l'adresse
        contexte.load_verify_locations(self.ac)
        with contexte.wrap_socket(socket.create_connection((hote, port), timeout=8)) as liaison:
            sujet = dict(x[0] for x in liaison.getpeercert().get("subject", ()))
            if sujet.get("commonName") != serie:
                raise Refus("l'appareil à cette adresse n'est pas l'imprimante attendue")
            liaison.sendall(paquet_mqtt(0x10, chaine_mqtt("MQTT") + bytes([4, 0xC2]) + struct.pack("!H", 60)
                                        + chaine_mqtt("blokfil-%d" % os.getpid()) + chaine_mqtt("bblp")
                                        + chaine_mqtt(code)))
            flux = Flux(liaison)
            reponse = flux.paquet()
            if not reponse or reponse[0] != 0x20 or len(reponse[1]) < 2:
                raise ConnectionError("pas de réponse de l'imprimante")
            if reponse[1][1] != 0:
                raise Refus("code d'accès refusé")
            if not self.adresse and hote != self.ip:
                self.ip = hote
                self.garder()
            liaison.sendall(paquet_mqtt(0x82, struct.pack("!H", 1) + chaine_mqtt("device/%s/report" % serie) + b"\x00"))
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
        ams = (donnees.get("print") or {}).get("ams") if isinstance(donnees, dict) else None
        if not isinstance(ams, dict):
            return
        avant = {c: e.get("present") for c, e in self.emplacements.items()}
        memoire = json.dumps(self.emplacements, sort_keys=True)
        for unite in ams.get("ams") or []:
            for bac in unite.get("tray") or []:
                try:
                    cle = "%d-%d" % (int(unite.get("id", 0)), int(bac.get("id", 0)))
                except (TypeError, ValueError):
                    continue
                e = self.emplacements.setdefault(cle, {})
                if "tray_type" in bac:
                    e["type"] = bac["tray_type"] or None
                if "tray_color" in bac:
                    e["couleur"] = couleur_propre(bac["tray_color"]) if bac["tray_color"] else None
        try:
            bits = int(str(ams["tray_exist_bits"]), 16)
        except (KeyError, ValueError):
            bits = None
        if bits is not None:
            unites = {0} | {int(c.partition("-")[0]) for c in self.emplacements} | {i // 4 for i in range(16) if bits >> i & 1}
            for u in unites:
                for b in range(4):
                    self.emplacements.setdefault("%d-%d" % (u, b), {})["present"] = bool(bits >> (u * 4 + b) & 1)
        for cle, e in self.emplacements.items():
            ancien = avant.get(cle)
            if ancien is not None and e.get("present") is not None and e["present"] != ancien:
                self.attente.setdefault(cle, {"depart": ancien})["dernier"] = time.time()
                print("emplacement", nom_emplacement(cle), ":", "chargé" if e["present"] else "retiré", flush=True)
        if json.dumps(self.emplacements, sort_keys=True) != memoire:
            self.garder()

    def garder(self):
        ecrire_json(self.donnees, "imprimante.json", {"ip": self.ip, "emplacements": self.emplacements})

    def verifier(self):
        """Annonce les changements une fois stabilisés : un retrait suivi d'une pose ne fait qu'une fenêtre."""
        maintenant, lignes = time.time(), []
        for cle in list(self.attente):
            a = self.attente[cle]
            present = self.emplacements.get(cle, {}).get("present")
            calme = maintenant - a["dernier"]
            if present and calme >= self.stabilisation:
                quoi = "bobine changée" if a["depart"] else "bobine chargée"
            elif not present and calme >= self.remplacement:
                quoi = "bobine retirée ou terminée" if a["depart"] else None
            else:
                continue
            del self.attente[cle]
            if quoi:
                lignes.append("Emplacement %s : %s." % (nom_emplacement(cle), quoi))
        if lignes:
            threading.Thread(target=self.fenetre, args=("\n".join(sorted(lignes)),), daemon=True).start()

    def fenetre(self, texte):
        print("fenêtre :", texte.replace("\n", " "), flush=True)
        if self.fenetre_essai:
            with open(self.fenetre_essai, "a", encoding="utf-8") as f:
                f.write(texte.replace("\n", " | ") + "\n")
            return
        message = " & return & ".join('"%s"' % l.replace("\\", "\\\\").replace('"', '\\"')
                                      for l in texte.split("\n") + ["", "Pensez à mettre la page des bobines à jour."])
        with self.fenetres:  # une seule fenêtre à la fois
            r = subprocess.run(["/usr/bin/osascript", "-e", "tell me to activate", "-e",
                                'display dialog %s with title "Fil par couleur" buttons {"Plus tard", "Ouvrir la page"} '
                                'default button 2' % message], capture_output=True, text=True)
        if "Ouvrir la page" in r.stdout:
            subprocess.run(["/usr/bin/open", "http://127.0.0.1:%d/" % self.port])


# ---------------------------------------------------------------- lancement

def main():
    a = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    a.add_argument("--racine", default=None, help="dossier temporaire à surveiller (essais)")
    a.add_argument("--donnees", default=DONNEES, help="dossier du journal et de la page")
    a.add_argument("--port", type=int, default=PORT, help="port de la page sur ce Mac")
    a.add_argument("--une-fois", action="store_true", help="deux passages puis arrêt, sans serveur (essais)")
    a.add_argument("--page", action="store_true", help="refaire seulement la copie de la page")
    a.add_argument("--couleurs", metavar="DOSSIER", help="relever les couleurs des projets Bambu de ce dossier")
    a.add_argument("--sans-imprimante", action="store_true", help="ne pas écouter l'imprimante")
    a.add_argument("--imprimante", metavar="ADRESSE:PORT", help="adresse imposée de l'imprimante (essais)")
    a.add_argument("--bambu-conf", default=BAMBU_CONF, help="réglages de Bambu Studio à lire (essais)")
    a.add_argument("--bambu-ac", default=BAMBU_AC, help="certificat qui reconnaît l'imprimante (essais)")
    a.add_argument("--fenetre-essai", metavar="FICHIER", help="écrire les fenêtres dans ce fichier au lieu de les afficher")
    a.add_argument("--delais", default="%d,%d" % (STABILISATION, REMPLACEMENT), help="délais d'annonce en secondes (essais)")
    o = a.parse_args()
    os.makedirs(o.donnees, exist_ok=True)
    if o.couleurs:
        print("couleurs relevées :", len(relever_couleurs(o.couleurs, o.donnees)))
    ecrire_page(o.donnees, o.port)
    if o.page or o.couleurs:
        return
    veilleur = Veilleur(o.racine or dossier_temporaire(), o.donnees, o.port)
    print("surveillance de", os.path.join(veilleur.racine, "bamboo_model"), flush=True)
    if o.une_fois:
        veilleur.passage()
        time.sleep(0.1)
        print("ajouts :", veilleur.passage())
        return
    imprimante = None
    if not o.sans_imprimante:
        adresse = None
        if o.imprimante:
            hote, _, port_imprimante = o.imprimante.rpartition(":")
            adresse = (hote, int(port_imprimante))
        imprimante = Imprimante(o.donnees, o.port, o.bambu_conf, o.bambu_ac, adresse, o.fenetre_essai,
                                tuple(float(x) for x in o.delais.split(",")))
        imprimante.start()
    serveur, essai = None, 0
    while True:
        if serveur is None and time.time() >= essai:
            try:
                serveur = lancer_serveur(o.donnees, o.port, imprimante)
                print("page sur http://127.0.0.1:%d/" % o.port, flush=True)
            except OSError as e:  # port occupé : la surveillance continue, on réessaie plus tard
                print("page indisponible :", e, file=sys.stderr, flush=True)
                essai = time.time() + 30
        try:
            veilleur.passage()
        except Exception as e:  # ne jamais s'arrêter sur un fichier inattendu
            print("erreur :", repr(e), file=sys.stderr, flush=True)
        time.sleep(PAUSE)


if __name__ == "__main__":
    main()
