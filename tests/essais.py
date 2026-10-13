"""Essais de BlokBambu, les mêmes sur macOS, Windows et Linux.

    python tests/essais.py              le compteur lancé à part, sur de fausses données et une fausse imprimante
    python tests/essais.py --installe   après l'installeur : le compteur installé répond et compte une impression
    python tests/essais.py --retire     après le retrait : plus rien ne tourne

Aucune dépendance. La fausse imprimante a besoin de la commande openssl pour ses certificats d'essai.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

DEPOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, DEPOT)
import compteur  # noqa: E402

COMPTEUR = os.path.join(DEPOT, "compteur.py")
SERIE, CODE = "ESSAI0000000001", "codeessai"
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


def page(port, chemin="/", donnees=None, entetes=None):
    """(code HTTP, texte) d'une requête à la page du compteur, sans suivre les redirections."""
    requete = urllib.request.Request("http://127.0.0.1:%d%s" % (port, chemin), data=donnees, headers=entetes or {})

    class SansRedirection(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    try:
        with urllib.request.build_opener(SansRedirection).open(requete, timeout=5) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except OSError:
        return 0, ""


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


# ---------------------------------------------------------------- le compteur lancé à part

def essais_a_part():
    base = tempfile.mkdtemp(prefix="blokbambu-essais-")
    racine, donnees = os.path.join(base, "tmp"), os.path.join(base, "donnees")
    os.makedirs(racine)
    port, port_imprimante = 47816, 48883
    processus = []
    try:
        print("Chemins par défaut sur ce système")
        chemins = json.loads(subprocess.check_output([sys.executable, COMPTEUR, "--chemins"]).decode("utf-8"))
        print("   ", chemins)
        verifier("dossier des données au nom de BlokBambu", "blokbambu" in chemins["donnees"].lower())
        verifier("réglages de Bambu Studio cherchés au bon nom", chemins["reglages_bambu"].endswith("BambuStudio.conf"))
        verifier("dossier temporaire du système surveillé",
                 any(os.path.realpath(r) == os.path.realpath(tempfile.gettempdir()) for r in chemins["dossiers_de_travail"]))

        print("Certificats d'essai et fausse imprimante")
        ac, cle_ac = os.path.join(base, "ac.pem"), os.path.join(base, "ac.key")
        cert, cle = os.path.join(base, "imp.pem"), os.path.join(base, "imp.key")
        demande = os.path.join(base, "imp.csr")
        silence = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.check_call(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", cle_ac, "-out", ac,
                               "-days", "2", "-subj", "/CN=AC d essai"], **silence)
        subprocess.check_call(["openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", cle, "-out", demande,
                               "-subj", "/CN=" + SERIE], **silence)
        subprocess.check_call(["openssl", "x509", "-req", "-in", demande, "-CA", ac, "-CAkey", cle_ac,
                               "-CAcreateserial", "-out", cert, "-days", "2"], **silence)
        dictee = os.path.join(base, "dictee.jsonl")
        journal_imprimante = open(os.path.join(base, "imprimante.log"), "w+", encoding="utf-8")
        processus.append(subprocess.Popen([sys.executable, os.path.join(DEPOT, "tests", "faux_imprimante.py"),
                                           str(port_imprimante), cert, cle, SERIE, CODE, dictee],
                                          stdout=journal_imprimante, stderr=subprocess.STDOUT))
        conf = os.path.join(base, "BambuStudio.conf")
        with open(conf, "w", encoding="utf-8") as f:
            json.dump({"app": {"user_last_selected_machine": SERIE}, "user_access_code": {SERIE: CODE}}, f)
        fenetres = os.path.join(base, "fenetres.txt")
        journal_compteur = open(os.path.join(base, "compteur.log"), "w+", encoding="utf-8")
        processus.append(lancer(["--racine", racine, "--donnees", donnees, "--port", str(port),
                                 "--imprimante", "127.0.0.1:%d" % port_imprimante, "--bambu-conf", conf,
                                 "--bambu-ac", ac, "--fenetre-essai", fenetres, "--delais", "2,6"], journal_compteur))

        print("Page")
        verifier("la page répond", attendre(lambda: page(port)[0] == 200))
        verifier("la page porte le nom BlokBambu", "<title>BlokBambu</title>" in page(port)[1])
        verifier("le logo et l'icône sont servis", page(port, "/logo.png")[0] == 200 and page(port, "/icone.png")[0] == 200)
        verifier("les données de la page sont servies", etat(port)["resume"]["impressions"] == 0)

        print("Impression lancée depuis Bambu Studio")
        envoi(racine, "12_00_00#1#1", 1, [("#161616", "PLA", 10.83), ("#FFFFFFFF", "PLA", 0.83)])
        entrees = attendre(lambda: journal(donnees))
        verifier("l'impression est comptée", bool(entrees))
        if entrees:
            e = entrees[0]
            verifier("une seule entrée (le fichier de réglages voisin est ignoré)", len(entrees) == 1, str(len(entrees)))
            verifier("nom du projet", e["projet"] == "Mon projet d'essai", e["projet"])
            verifier("grammes et couleurs",
                     [(f["couleur"], f["g"]) for f in e["fils"]] == [("#161616", 10.83), ("#FFFFFF", 0.83)], str(e["fils"]))
            verifier("nom du fil", e["fils"][0]["nom"] == "Fil 1", e["fils"][0]["nom"])

        print("Bobines")
        origine = {"Origin": "http://127.0.0.1:%d" % port}
        code, _ = page(port, "/bobine", b"couleur=%23161616&type=PLA&poids=1000&marque=eSUN", origine)
        verifier("ajout d'une bobine", code == 200, str(code))
        verifier("la marque de la bobine est gardée", etat(port)["bobines"][0]["marque"] == "eSUN")
        verifier("poids invalide refusé", page(port, "/bobine", b"couleur=%23161616&type=PLA&poids=-5", origine)[0] == 400)
        verifier("couleur invalide refusée", page(port, "/bobine", b"couleur=noir&type=PLA&poids=10", origine)[0] == 400)
        verifier("envoi venu d'un autre site refusé",
                 page(port, "/bobine", b"couleur=%23161616&type=PLA&poids=5", {"Origin": "http://exemple.fr"})[0] == 403)
        verifier("envoi sans origine refusé", page(port, "/bobine", b"couleur=%23161616&type=PLA&poids=5")[0] == 403)
        verifier("page demandée sous un autre nom refusée", page(port, "/", None, {"Host": "exemple.fr"})[0] == 403)
        time.sleep(1.2)  # la date de la bobine est à la seconde : l'impression suivante doit venir après
        envoi(racine, "12_00_00#1#1", 1, [("#161616", "PLA", 10.83), ("#FFFFFFFF", "PLA", 0.83)])
        verifier("une réimpression du même plateau compte une deuxième fois", attendre(lambda: len(journal(donnees)) == 2))
        verifier("la bobine se déduit de l'impression", attendre(lambda: etat(port)["bobines"][0]["reste"] == 989.17),
                 str(etat(port)["bobines"]))
        verifier("le blanc, imprimé sans bobine, est proposé au suivi",
                 [b["cle"] for b in etat(port)["a_suivre"]] == ["#FFFFFF|PLA"], str(etat(port)["a_suivre"]))
        page(port, "/bobine", b"couleur=%23161616&type=PLA&poids=1000&marque=Ma+Marque+Maison", origine)
        b = etat(port)["bobines"][0]
        verifier("« Nouvelle bobine » remet à neuf", b["reste"] == 1000 and b["part"] == 1, str(b))
        verifier("une marque nouvelle est retenue et proposée ensuite",
                 b["marque"] == "Ma Marque Maison" and "Ma Marque Maison" in etat(port)["marques"])
        verifier("les marques de base sont proposées", {"Bambu Lab", "eSUN", "Polymaker"} <= set(etat(port)["marques"]))
        verifier("marque devinée d'après le nom du fil dans Bambu Studio",
                 compteur.marque_devinee(["eSUN PLA+"]) == "eSUN" and compteur.marque_devinee(["PolyTerra PLA"]) == "Polymaker"
                 and compteur.marque_devinee(["Generic PLA"]) == "")
        page(port, "/retirer", b"couleur=%23161616&type=PLA", origine)
        verifier("retrait d'une bobine", etat(port)["bobines"] == [])

        print("Imprimante")
        verifier("liaison établie avec la fausse imprimante",
                 attendre(lambda: etat(port)["imprimante"]["etat"] == "connectee"))

        def dicter(ligne):
            with open(dictee, "a", encoding="utf-8") as f:
                f.write(ligne + "\n")

        def vues():
            try:
                with open(fenetres, encoding="utf-8") as f:
                    return f.read()
            except OSError:
                return ""

        bacs = ",".join('{"id":"%d","tray_type":"PLA","tray_color":"%s"}' % (i, c)
                        for i, c in enumerate(("76D9F4FF", "FFFFFFFF", "161616FF", "F98C36FF")))
        dicter('{"print":{"ams":{"ams":[{"id":"0","tray":[%s]}],"ams_exist_bits":"1","tray_exist_bits":"f"}}}' % bacs)
        dicter('{"print":{"nozzle_temper":25.1}}')
        verifier("les bobines chargées sont proposées sur la page, avec leur emplacement",
                 attendre(lambda: {b["cle"]: b["emplacements"] for b in etat(port)["a_suivre"]}.get("#161616|PLA") == ["A3"]))
        dicter('{"print":{"ams":{"tray_exist_bits":"d"}}}')
        time.sleep(1.5)
        dicter('{"print":{"ams":{"tray_exist_bits":"f"}}}')
        verifier("bobine retirée puis remise : une seule fenêtre « changée »",
                 attendre(lambda: "Emplacement A2 : bobine changée." in vues()) and vues().count("A2") == 1, vues())
        dicter('{"print":{"ams":{"tray_exist_bits":"b"}}}')
        verifier("bobine retirée sans remplacement", attendre(lambda: "Emplacement A3 : bobine retirée ou terminée." in vues()), vues())
        dicter('{"print":{"ams":{"tray_exist_bits":"f"}}}')
        verifier("bobine chargée", attendre(lambda: "Emplacement A3 : bobine chargée." in vues()), vues())
        journal_imprimante.seek(0)
        echanges = journal_imprimante.read()
        verifier("le code d'accès est accepté", "CONNECT, code juste" in echanges, echanges)
        verifier("le compteur n'a rien publié vers l'imprimante", "COMMANDE" not in echanges, echanges)

        print("Vrai dossier temporaire du système")
        if page(compteur.PORT)[0]:
            print("  (sauté : un BlokBambu installé tourne ici, il compterait cet essai)")
        else:
            donnees2 = os.path.join(base, "donnees2")
            processus.append(lancer(["--donnees", donnees2, "--port", "47818", "--sans-imprimante"], journal_compteur))
            attendre(lambda: page(47818)[0] == 200)
            dossier = envoi(tempfile.gettempdir(), "essai#0#0", 7, [("#00AE42", "PETG", 42.5)], origine="")
            entrees = attendre(lambda: journal(donnees2))
            verifier("impression repérée dans le dossier temporaire par défaut", bool(entrees))
            if entrees:
                verifier("sans projet enregistré, le nom vient de l'objet", entrees[0]["projet"] == "Cube", entrees[0]["projet"])
            shutil.rmtree(dossier, ignore_errors=True)
    finally:
        for p in processus:
            p.terminate()
        for p in processus:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        if echecs:
            for nom in ("compteur.log", "imprimante.log"):
                try:
                    with open(os.path.join(base, nom), encoding="utf-8") as f:
                        print("----- %s\n%s" % (nom, f.read()[-3000:]))
                except OSError:
                    pass
        shutil.rmtree(base, ignore_errors=True)


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
    dossier = envoi(tempfile.gettempdir(), "essai#0#0", 9, [("#F98C36", "PLA", 12.5)])
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
