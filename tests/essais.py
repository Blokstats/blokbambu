"""Essais de BlokBambu, les mêmes sur macOS, Windows et Linux.

    python tests/essais.py              le compteur lancé à part, sur de fausses données et de fausses imprimantes
    python tests/essais.py --installe   après l'installeur : le compteur installé répond et compte une impression
    python tests/essais.py --retire     après le retrait : plus rien ne tourne

Aucune dépendance. Les fausses imprimantes ont besoin de la commande openssl pour leurs certificats d'essai.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

DEPOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, DEPOT)
import compteur  # noqa: E402

COMPTEUR = os.path.join(DEPOT, "compteur.py")
SERIE, SERIE2, CODE = "ESSAI0000000001", "ESSAI0000000002", "codeessai"
NOIR, BLANC, BLEU, ORANGE = "#161616", "#FFFFFF", "#76D9F4", "#F98C36"
echecs = []


def verifier(nom, condition, detail=""):
    print(("  ok    " if condition else "  ÉCHEC ") + nom + (" — %s" % detail if detail and not condition else ""), flush=True)
    if not condition:
        echecs.append(nom)


def attendre(condition, delai=25):
    fin = time.time() + delai
    while time.time() < fin:
        try:
            valeur = condition()
        except Exception:
            valeur = None
        if valeur:
            return valeur
        time.sleep(0.4)
    return None


def page(port, chemin="/", donnees=None, entetes=None, hote="127.0.0.1"):
    """(code HTTP, texte, en-têtes) d'une requête à la page du compteur, sans suivre les redirections."""
    requete = urllib.request.Request("http://%s:%d%s" % (hote, port, chemin), data=donnees, headers=entetes or {})

    class SansRedirection(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    try:
        with urllib.request.build_opener(SansRedirection).open(requete, timeout=5) as r:
            return r.status, r.read().decode("utf-8", "replace"), r.headers
    except urllib.error.HTTPError as e:
        return e.code, "", e.headers
    except OSError:
        return 0, "", {}


def etat(port):
    """Ce que la page affiche : les données que le compteur lui sert."""
    return json.loads(page(port, "/api/etat")[1])


def envoi(racine, session, numero, fils, origine="Mon projet d'essai.3mf", plateau=1):
    """Pose le fichier que Bambu Studio écrit quand on lance une impression."""
    dossier = os.path.join(racine, "bamboo_model", "Mon_Oct_12", session)
    os.makedirs(os.path.join(dossier, "Metadata"), exist_ok=True)
    if origine:
        with open(os.path.join(dossier, "origin.txt"), "w", encoding="utf-8") as f:
            f.write(os.path.join(os.path.expanduser("~"), "Projets", origine))
    lignes = "".join('<filament id="%d" type="%s" color="%s" used_m="%.2f" used_g="%.2f"/>' % (i + 1, t, c, g / 3, g)
                     for i, (c, t, g) in enumerate(fils))
    infos = ('<?xml version="1.0" encoding="UTF-8"?><config><plate><metadata key="index" value="%d"/>'
             '<metadata key="prediction" value="2114"/><object identify_id="1" name="Cube"/>%s</plate></config>'
             % (plateau, lignes))
    chemin = os.path.join(dossier, "Metadata", ".%d.4.3mf" % numero)
    provisoire = chemin + ".ecriture"
    with zipfile.ZipFile(provisoire, "w") as z:
        z.writestr("Metadata/slice_info.config", infos)
        z.writestr("Metadata/plate_%d.gcode" % plateau, "; essai\n")
        z.writestr("Metadata/project_settings.config",
                   json.dumps({"filament_settings_id": ["Fil %d @BBL A1" % (i + 1) for i in range(len(fils))]}))
    for essai in range(10):  # sous Windows, le compteur peut être en train de lire l'ancien fichier
        try:
            os.replace(provisoire, chemin)
            break
        except PermissionError:
            time.sleep(0.3)
    # Fichiers voisins que le compteur doit laisser de côté.
    with zipfile.ZipFile(os.path.join(dossier, "Metadata", ".%d.5_config.3mf" % numero), "w") as z:
        z.writestr("Metadata/slice_info.config", infos)
    return dossier


def journal(donnees):
    try:
        with open(os.path.join(donnees, "impressions.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def lancer(arguments, sortie):
    return subprocess.Popen([sys.executable, COMPTEUR] + arguments, stdout=sortie, stderr=subprocess.STDOUT)


class Banc:
    """Un dossier d'essai : certificats, fausses imprimantes, exemplaires du compteur, tout arrêté à la fin."""

    def __init__(self):
        self.base = tempfile.mkdtemp(prefix="blokbambu-essais-")
        self.processus, self.journaux = [], []
        self.ac, self.cle_ac = self.chemin("ac.pem"), self.chemin("ac.key")
        silence = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.check_call(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", self.cle_ac,
                               "-out", self.ac, "-days", "2", "-subj", "/CN=AC d essai"], **silence)

    def chemin(self, *noms):
        return os.path.join(self.base, *noms)

    def sortie(self, nom):
        f = open(self.chemin(nom), "a+", encoding="utf-8")
        self.journaux.append(nom)
        return f

    def imprimante(self, serie, port):
        """Lance une fausse imprimante. Rend la fonction qui lui dicte ses annonces."""
        cert, cle, demande = self.chemin(serie + ".pem"), self.chemin(serie + ".key"), self.chemin(serie + ".csr")
        silence = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.check_call(["openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", cle, "-out", demande,
                               "-subj", "/CN=" + serie], **silence)
        subprocess.check_call(["openssl", "x509", "-req", "-in", demande, "-CA", self.ac, "-CAkey", self.cle_ac,
                               "-CAcreateserial", "-out", cert, "-days", "2"], **silence)
        dictee = self.chemin(serie + ".jsonl")
        self.processus.append(subprocess.Popen(
            [sys.executable, os.path.join(DEPOT, "tests", "faux_imprimante.py"), str(port), cert, cle, serie, CODE, dictee],
            stdout=self.sortie(serie + ".log"), stderr=subprocess.STDOUT))

        def dicter(annonce):
            with open(dictee, "a", encoding="utf-8") as f:
                f.write(json.dumps({"print": annonce}) + "\n")
        return dicter

    def conf(self, series):
        chemin = self.chemin("BambuStudio-%d.conf" % len(series))
        with open(chemin, "w", encoding="utf-8") as f:
            json.dump({"app": {"user_last_selected_machine": series[0]}, "access_code": {s: CODE for s in series}}, f)
        return chemin

    def compteur(self, arguments):
        self.processus.append(lancer(arguments, self.sortie("compteur.log")))

    def lu(self, nom):
        try:
            with open(self.chemin(nom), encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""

    def fermer(self):
        for p in self.processus:
            p.terminate()
        for p in self.processus:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        if echecs:
            for nom in self.journaux:
                print("----- %s\n%s" % (nom, self.lu(nom)[-2500:]))
        shutil.rmtree(self.base, ignore_errors=True)


def bacs(*couleurs, **plus):
    """L'annonce complète d'un AMS chargé de ces couleurs."""
    liste = []
    for i, c in enumerate(couleurs):
        bac = {"id": str(i), "tray_type": "PLA", "tray_color": c[1:] + "FF", "tag_uid": "0000000000000000",
               "tray_weight": "0", "remain": 100, "tray_info_idx": "GFL99"}
        bac.update(plus.get("bac%d" % i, {}))
        liste.append(bac)
    return {"ams": [{"id": "0", "tray": liste}], "ams_exist_bits": "1", "tray_exist_bits": "f", "tray_now": "255"}


# ---------------------------------------------------------------- le compteur lancé à part

def essais_a_part():
    banc = Banc()
    racine, donnees = banc.chemin("tmp"), banc.chemin("donnees")
    os.makedirs(racine)
    port = 47816
    origine = {"Origin": "http://127.0.0.1:%d" % port}

    def poster(chemin, champs):
        return page(port, chemin, urllib.parse.urlencode(champs).encode("utf-8"), origine)[0]

    def impression(projet):
        return next((i for i in etat(port)["impressions"] if i["projet"] == projet), None)

    def bobine(couleur):
        return next((b for b in etat(port)["bobines"] if b["couleur"] == couleur), None)

    try:
        print("Chemins par défaut sur ce système")
        chemins = json.loads(subprocess.check_output([sys.executable, COMPTEUR, "--chemins"]).decode("utf-8"))
        print("   ", chemins)
        verifier("dossier des données au nom de BlokBambu", "blokbambu" in chemins["donnees"].lower())
        verifier("réglages de Bambu Studio cherchés au bon nom", chemins["reglages_bambu"].endswith("BambuStudio.conf"))
        verifier("dossier temporaire du système surveillé",
                 any(os.path.realpath(r) == os.path.realpath(tempfile.gettempdir()) for r in chemins["dossiers_de_travail"]))

        dicter = banc.imprimante(SERIE, 48883)
        fenetres = banc.chemin("fenetres.txt")
        banc.compteur(["--racine", racine, "--donnees", donnees, "--port", str(port), "--imprimante", "127.0.0.1:48883",
                       "--bambu-conf", banc.conf([SERIE]), "--bambu-ac", banc.ac, "--fenetre-essai", fenetres,
                       "--delais", "2,6"])

        def vues():
            return banc.lu("fenetres.txt")

        print("Page")
        verifier("la page répond", attendre(lambda: page(port)[0] == 200))
        verifier("la page porte le nom BlokBambu", "<title>BlokBambu</title>" in page(port)[1])
        verifier("la police des titres est servie", page(port, "/police.woff2")[0] == 200)
        verifier("les données de la page sont servies", etat(port)["resume"]["impressions"] == 0)

        print("Impression lancée depuis Bambu Studio")
        envoi(racine, "12_00_00#1#1", 1, [(NOIR, "PLA", 10.83), ("#FFFFFFFF", "PLA", 0.83)])
        entrees = attendre(lambda: journal(donnees))
        verifier("l'impression est comptée", bool(entrees))
        if entrees:
            e = entrees[0]
            verifier("une seule entrée (le fichier de réglages voisin est ignoré)", len(entrees) == 1, str(len(entrees)))
            verifier("nom du projet", e["projet"] == "Mon projet d'essai", e["projet"])
            verifier("grammes et couleurs",
                     [(f["couleur"], f["g"]) for f in e["fils"]] == [(NOIR, 10.83), (BLANC, 0.83)], str(e["fils"]))
            verifier("nom du fil", e["fils"][0]["nom"] == "Fil 1", e["fils"][0]["nom"])

        print("Bobines")
        verifier("ajout d'une bobine, avec sa marque, son prix et sa réserve",
                 poster("/bobine", {"couleur": NOIR, "type": "PLA", "poids": "1000", "marque": "eSUN", "prix": "20", "reserve": "2"}) == 200)
        b = bobine(NOIR)
        verifier("marque, prix et réserve gardés", (b["marque"], b["prix"], b["reserve"]) == ("eSUN", 20, 2), str(b))
        verifier("poids invalide refusé", poster("/bobine", {"couleur": NOIR, "type": "PLA", "poids": "-5"}) == 400)
        verifier("couleur invalide refusée", poster("/bobine", {"couleur": "noir", "type": "PLA", "poids": "10"}) == 400)
        verifier("prix invalide refusé", poster("/bobine", {"couleur": NOIR, "type": "PLA", "poids": "10", "prix": "cher"}) == 400)
        verifier("envoi venu d'un autre site refusé",
                 page(port, "/bobine", b"couleur=%23161616&type=PLA&poids=5", {"Origin": "http://exemple.fr"})[0] == 403)
        verifier("envoi sans origine refusé", page(port, "/bobine", b"couleur=%23161616&type=PLA&poids=5")[0] == 403)
        verifier("page demandée sous un autre nom refusée", page(port, "/", None, {"Host": "exemple.fr"})[0] == 403)
        time.sleep(1.2)  # la date de la bobine est à la seconde : l'impression suivante doit venir après
        envoi(racine, "12_00_00#1#1", 1, [(NOIR, "PLA", 10.83), ("#FFFFFFFF", "PLA", 0.83)])
        verifier("une réimpression du même plateau compte une deuxième fois", attendre(lambda: len(journal(donnees)) == 2))
        verifier("la bobine se déduit de l'impression", attendre(lambda: bobine(NOIR)["reste"] == 989.17), str(bobine(NOIR)))
        verifier("le blanc, imprimé sans bobine, est proposé au suivi",
                 [x["cle"] for x in etat(port)["a_suivre"]] == [BLANC + "|PLA"], str(etat(port)["a_suivre"]))

        print("Coût")
        derniere = etat(port)["impressions"][0]
        verifier("coût de l'impression d'après le prix de la bobine", derniere["cout"] == 0.22, str(derniere))
        verifier("l'impression d'avant la bobine n'a pas de coût", etat(port)["impressions"][1]["cout"] is None)
        verifier("coût du mois et total", etat(port)["resume"]["mois"]["cout"] == 0.22 and etat(port)["resume"]["cout"] == 0.22,
                 str(etat(port)["resume"]))
        verifier("modifier le prix sans toucher au reste",
                 poster("/bobine/modifier", {"couleur": NOIR, "type": "PLA", "marque": "eSUN", "prix": "30", "reserve": "2"}) == 200
                 and bobine(NOIR)["reste"] == 989.17 and bobine(NOIR)["prix"] == 30)
        verifier("le prix corrigé vaut pour les impressions de cette bobine", etat(port)["impressions"][0]["cout"] == 0.32,
                 str(etat(port)["impressions"][0]))
        mois = etat(port)["mois"]
        verifier("total du mois", len(mois) == 1 and mois[0]["impressions"] == 2 and mois[0]["g"] == 23.32, str(mois))

        print("Corriger une impression")
        verifier("part imprimée ramenée à 50 %", poster("/impression", {"id": derniere["id"], "part": "50"}) == 200)
        corrigee = etat(port)["impressions"][0]
        verifier("le fil compté suit la part", corrigee["part"] == 0.5 and corrigee["fils"][0]["g"] == 5.42 and corrigee["cout"] == 0.16,
                 str(corrigee))
        verifier("la bobine rend le fil non imprimé", abs(bobine(NOIR)["reste"] - 994.585) < 0.011, str(bobine(NOIR)))
        verifier("part hors bornes refusée", poster("/impression", {"id": derniere["id"], "part": "140"}) == 400)
        verifier("impression supprimée", poster("/impression/supprimer", {"id": derniere["id"]}) == 200
                 and len(etat(port)["impressions"]) == 1 and bobine(NOIR)["reste"] == 1000)
        time.sleep(7)
        verifier("une impression supprimée n'est pas recomptée", len(etat(port)["impressions"]) == 1)

        print("Nouvelle bobine, marques")
        poster("/bobine", {"couleur": NOIR, "type": "PLA", "poids": "1000", "marque": "Ma Marque Maison", "prix": "30", "reserve": "1"})
        b = bobine(NOIR)
        verifier("« Nouvelle bobine » remet à neuf", b["reste"] == 1000 and b["part"] == 1, str(b))
        verifier("une marque nouvelle est retenue et proposée ensuite",
                 b["marque"] == "Ma Marque Maison" and "Ma Marque Maison" in etat(port)["marques"])
        verifier("les marques de base sont proposées", {"Bambu Lab", "eSUN", "Polymaker"} <= set(etat(port)["marques"]))
        verifier("marque devinée d'après le nom du fil dans Bambu Studio",
                 compteur.marque_devinee(["eSUN PLA+"]) == "eSUN" and compteur.marque_devinee(["PolyTerra PLA"]) == "Polymaker"
                 and compteur.marque_devinee(["Generic PLA"]) == "")
        poster("/retirer", {"couleur": NOIR, "type": "PLA"})
        verifier("retrait d'une bobine", etat(port)["bobines"] == [])

        print("Alertes au lancement d'une impression")
        poster("/bobine", {"couleur": ORANGE, "type": "PLA", "poids": "5"})
        poster("/bobine", {"couleur": BLEU, "type": "PLA", "poids": "105", "reserve": "0"})
        time.sleep(1.2)
        envoi(racine, "12_10_00#1#2", 2, [(ORANGE, "PLA", 12.0), (BLEU, "PLA", 10.0)], origine="Trop gros.3mf")
        verifier("pas assez de fil : une fenêtre le dit",
                 attendre(lambda: "Orange : il faut 12 g, il en reste environ 5 g sur la bobine." in vues()), vues())
        verifier("dernière bobine bientôt vide : la fenêtre le dit aussi",
                 "Bleu ciel : bientôt vide, et plus de bobine en réserve." in vues(), vues())
        verifier("la page liste ce qu'il faut racheter", attendre(lambda: etat(port)["a_racheter"] == ["Bleu ciel PLA"]),
                 str(etat(port)["a_racheter"]))
        alertes = etat(port)["alertes"]
        verifier("l'alerte est aussi affichée dans la page", len(alertes) == 1 and "Orange : il faut 12 g" in alertes[0]["texte"],
                 str(alertes))
        verifier("une alerte se ferme depuis la page", poster("/alerte/fermer", {"id": alertes[0]["id"]}) == 200
                 and etat(port)["alertes"] == [])
        poster("/retirer", {"couleur": ORANGE, "type": "PLA"})
        poster("/retirer", {"couleur": BLEU, "type": "PLA"})

        print("Imprimante : bobines")
        verifier("liaison établie avec la fausse imprimante",
                 attendre(lambda: [i["etat"] for i in etat(port)["imprimantes"]] == ["connectee"]), str(etat(port)["imprimantes"]))
        dicter({"ams": bacs(BLEU, BLANC, NOIR, ORANGE)})
        dicter({"nozzle_temper": 25.1})
        verifier("les bobines chargées sont proposées sur la page, avec leur emplacement",
                 attendre(lambda: {x["cle"]: x["emplacements"] for x in etat(port)["a_suivre"]}.get(NOIR + "|PLA") == ["A3"]))
        dicter({"ams": {"tray_exist_bits": "d"}})
        time.sleep(1.5)
        dicter({"ams": {"tray_exist_bits": "f"}})
        verifier("bobine retirée puis remise : une seule fenêtre « changée »",
                 attendre(lambda: "Emplacement A2 : bobine changée." in vues()) and vues().count("A2") == 1, vues())
        dicter({"ams": {"tray_exist_bits": "b"}})
        verifier("bobine retirée sans remplacement", attendre(lambda: "Emplacement A3 : bobine retirée ou terminée." in vues()), vues())
        dicter({"ams": {"tray_exist_bits": "f"}})
        verifier("bobine chargée", attendre(lambda: "Emplacement A3 : bobine chargée." in vues()), vues())

        print("Imprimante : suivi d'une impression")
        envoi(racine, "12_20_00#1#3", 3, [(NOIR, "PLA", 20.0)], origine="Pièce seule.3mf")
        verifier("impression à un fil comptée", attendre(lambda: impression("Pièce seule")))
        dicter({"gcode_state": "PREPARE", "subtask_name": "Pièce seule_plate_1", "subtask_id": "4201", "mc_percent": 0})
        dicter({"gcode_state": "RUNNING", "mc_percent": 30, "ams": {"tray_now": "3"}})
        verifier("l'impression en cours est reconnue, avec son avancement",
                 attendre(lambda: impression("Pièce seule")["en_cours"] == 30), str(impression("Pièce seule")))
        verifier("le fil est rattaché à la bobine qui fournit vraiment (emplacement A4, orange)",
                 attendre(lambda: impression("Pièce seule")["fils"][0]["couleur"] == ORANGE), str(impression("Pièce seule")))
        dicter({"mc_percent": 40})
        dicter({"gcode_state": "FAILED"})
        arretee = attendre(lambda: impression("Pièce seule")["part"] == 0.4 and impression("Pièce seule"))
        verifier("impression arrêtée : seule la part faite compte", bool(arretee) and arretee["fils"][0]["g"] == 8.0
                 and arretee["en_cours"] is None, str(impression("Pièce seule")))

        envoi(racine, "12_30_00#1#4", 4, [(NOIR, "PLA", 6.0), (BLANC, "PLA", 4.0)], origine="Deux fils.3mf")
        verifier("impression à deux fils comptée", attendre(lambda: impression("Deux fils")))
        dicter({"gcode_state": "RUNNING", "subtask_name": "Deux fils_plate_1", "subtask_id": "4202", "mc_percent": 10,
                "ams": {"tray_now": "2"}})
        dicter({"mc_percent": 60, "ams": {"tray_now": "1"}})
        dicter({"gcode_state": "FINISH", "mc_percent": 100})
        time.sleep(4)
        finie = impression("Deux fils")
        verifier("impression menée au bout : elle compte en entier, chaque fil sur sa bobine",
                 finie["part"] == 1 and finie["en_cours"] is None
                 and [(f["couleur"], f["g"]) for f in finie["fils"]] == [(NOIR, 6.0), (BLANC, 4.0)], str(finie))

        print("Imprimante : bobine Bambu Lab à puce")
        dicter({"ams": bacs(BLEU, BLANC, NOIR, ORANGE, bac0={"tag_uid": "A1B2C3D400000100", "tray_weight": "1000",
                                                             "remain": 37, "tray_info_idx": "GFA00"})})
        puce = attendre(lambda: bobine(BLEU))
        verifier("le reste est lu sur la puce, sans saisie", bool(puce) and puce["source"] == "puce" and puce["reste"] == 370
                 and puce["marque"] == "Bambu Lab" and not puce["suivie"], str(puce))
        echanges = banc.lu(SERIE + ".log")
        verifier("le code d'accès est accepté", "CONNECT, code juste" in echanges, echanges)
        verifier("le compteur n'a rien publié vers l'imprimante", "COMMANDE" not in echanges, echanges)

        print("Accès depuis le téléphone")
        adresse = compteur.adresse_locale()
        if not adresse:
            print("  (sauté : cet ordinateur n'est sur aucun réseau local)")
        else:
            verifier("fermé au réseau tant que le réglage est coupé", page(port, "/", hote=adresse)[0] == 0)
            verifier("réglage activé depuis l'ordinateur", poster("/reglages", {"reseau": "1"}) == 200)
            reglages = etat(port)["reglages"]
            code = reglages["code"]
            verifier("un code à six chiffres et l'adresse à ouvrir sont donnés",
                     reglages["reseau"] and len(code) == 6 and code.isdigit() and reglages["adresse"] == "http://%s:%d/" % (adresse, port),
                     str(reglages))
            verifier("la page s'ouvre au réseau local", attendre(lambda: page(port, "/code", hote=adresse)[0] == 200, 15))
            verifier("sans le code, la page renvoie vers la demande de code",
                     page(port, "/", hote=adresse)[0] == 303 and page(port, "/api/etat", hote=adresse)[0] == 303)
            distant = {"Origin": "http://%s:%d" % (adresse, port)}
            faux = page(port, "/code", b"code=000000" if code != "000000" else b"code=111111", distant, hote=adresse)
            verifier("code faux refusé", faux[0] == 200 and "Ce n'est pas le bon code." in faux[1])
            bon = page(port, "/code", ("code=" + code).encode(), distant, hote=adresse)
            biscuit = (bon[2].get("Set-Cookie") or "").split(";")[0]
            verifier("bon code accepté", bon[0] == 303 and biscuit.startswith("blokbambu="), str(bon[0]))
            vu = page(port, "/api/etat", None, {"Cookie": biscuit}, hote=adresse)
            verifier("avec le code, le téléphone voit les données", vu[0] == 200 and json.loads(vu[1])["local"] is False
                     and "reglages" not in json.loads(vu[1]))
            verifier("le téléphone peut gérer les bobines",
                     page(port, "/bobine", b"couleur=%23161616&type=PLA&poids=800", dict(distant, Cookie=biscuit), hote=adresse)[0] == 200
                     and bobine(NOIR)["reste"] == 800)
            verifier("mais pas changer les réglages",
                     page(port, "/reglages", b"reseau=0", dict(distant, Cookie=biscuit), hote=adresse)[0] == 403)
            verifier("sous un autre nom que l'adresse locale, refus",
                     page(port, "/", None, {"Host": "exemple.fr:%d" % port, "Cookie": biscuit}, hote=adresse)[0] == 403)
            for _ in range(5):
                page(port, "/code", b"code=999999" if code != "999999" else b"code=111111", distant, hote=adresse)
            verifier("après cinq codes faux, les essais sont suspendus",
                     "Trop d'essais" in page(port, "/code", ("code=" + code).encode(), distant, hote=adresse)[1])
            poster("/reglages", {"reseau": "0"})
            verifier("réglage coupé : la page se referme au réseau", attendre(lambda: page(port, "/", hote=adresse)[0] == 0, 15))
            verifier("et répond toujours sur l'ordinateur", page(port)[0] == 200)

        print("Deux imprimantes")
        donnees2, fenetres2 = banc.chemin("donnees2"), banc.chemin("fenetres2.txt")
        dicter_a, dicter_b = banc.imprimante("ESSAI00000000A1", 48884), banc.imprimante(SERIE2, 48885)
        banc.compteur(["--racine", banc.chemin("vide"), "--donnees", donnees2, "--port", "47819",
                       "--imprimante", "ESSAI00000000A1=127.0.0.1:48884,%s=127.0.0.1:48885" % SERIE2,
                       "--bambu-conf", banc.conf(["ESSAI00000000A1", SERIE2]), "--bambu-ac", banc.ac,
                       "--fenetre-essai", fenetres2, "--delais", "2,6"])
        verifier("les deux imprimantes sont écoutées",
                 attendre(lambda: [i["etat"] for i in etat(47819)["imprimantes"]] == ["connectee", "connectee"]),
                 str(etat(47819)["imprimantes"]) if page(47819)[0] else "page muette")
        dicter_a({"ams": bacs(NOIR, BLANC, BLEU, ORANGE)})
        dicter_b({"ams": bacs(ORANGE, NOIR, BLANC, BLEU)})
        verifier("chaque bobine porte son imprimante et son emplacement",
                 attendre(lambda: {x["cle"]: x["emplacements"] for x in etat(47819)["a_suivre"]}.get(NOIR + "|PLA") == ["1·A1", "2·A2"]),
                 str(etat(47819)["a_suivre"]))
        dicter_b({"ams": {"tray_exist_bits": "e"}})
        time.sleep(1.5)
        dicter_b({"ams": {"tray_exist_bits": "f"}})
        verifier("la fenêtre dit de quelle imprimante il s'agit",
                 attendre(lambda: "Imprimante 2, emplacement A1 : bobine changée." in banc.lu("fenetres2.txt")), banc.lu("fenetres2.txt"))

        print("Vrai dossier temporaire du système")
        if page(compteur.PORT)[0]:
            print("  (sauté : un BlokBambu installé tourne ici, il compterait cet essai)")
        else:
            donnees3 = banc.chemin("donnees3")
            banc.compteur(["--donnees", donnees3, "--port", "47818", "--sans-imprimante"])
            attendre(lambda: page(47818)[0] == 200)
            dossier = envoi(tempfile.gettempdir(), "essai#0#0", 7, [("#00AE42", "PETG", 42.5)], origine="")
            entrees = attendre(lambda: journal(donnees3))
            verifier("impression repérée dans le dossier temporaire par défaut", bool(entrees))
            if entrees:
                verifier("sans projet enregistré, le nom vient de l'objet", entrees[0]["projet"] == "Cube", entrees[0]["projet"])
            shutil.rmtree(dossier, ignore_errors=True)
    finally:
        banc.fermer()


# ---------------------------------------------------------------- le compteur installé

def lancement_automatique():
    """True si le lancement à l'ouverture de session est en place."""
    if compteur.MAC:
        return os.path.exists(os.path.expanduser("~/Library/LaunchAgents/fr.blokcorp.blokbambu.plist"))
    if compteur.WINDOWS:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as cle:
                return "compteur.py" in winreg.QueryValueEx(cle, "BlokBambu")[0]
        except OSError:
            return False
    config = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.exists(os.path.join(config, "autostart", "blokbambu.desktop"))


def essais_installe():
    print("Compteur installé")
    verifier("la page répond sur le port habituel", attendre(lambda: page(compteur.PORT)[0] == 200, 40))
    verifier("lancement à l'ouverture de session en place", lancement_automatique())
    avant = len(journal(compteur.DONNEES))
    dossier = envoi(tempfile.gettempdir(), "essai#0#0", 9, [(ORANGE, "PLA", 12.5)])
    entrees = attendre(lambda: len(journal(compteur.DONNEES)) > avant and journal(compteur.DONNEES))
    verifier("une impression posée dans le dossier temporaire est comptée", bool(entrees))
    if entrees:
        verifier("grammes", entrees[-1]["fils"][0]["g"] == 12.5, str(entrees[-1]["fils"]))
    shutil.rmtree(dossier, ignore_errors=True)
    if echecs:
        try:
            with open(os.path.join(compteur.DONNEES, "compteur.log"), encoding="utf-8") as f:
                print("----- compteur.log\n" + f.read()[-3000:])
        except OSError as e:
            print("pas de journal :", e)


def essais_retire():
    print("Compteur retiré")
    verifier("la page ne répond plus", attendre(lambda: page(compteur.PORT)[0] == 0, 20))
    verifier("plus de lancement à l'ouverture de session", not lancement_automatique())
    verifier("les données sont gardées", os.path.exists(os.path.join(compteur.DONNEES, "impressions.json")))


if __name__ == "__main__":
    if "--installe" in sys.argv:
        essais_installe()
    elif "--retire" in sys.argv:
        essais_retire()
    else:
        essais_a_part()
    print("\n%s" % ("TOUT EST BON" if not echecs else "%d ÉCHEC(S) : %s" % (len(echecs), ", ".join(echecs))))
    sys.exit(1 if echecs else 0)
