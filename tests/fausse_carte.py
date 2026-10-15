"""Fausse carte mémoire d'imprimante pour les essais : FTP chiffré dès la connexion, comme sur une Bambu.

    python fausse_carte.py PORT CERTIFICAT CLE CODE DOSSIER

Sert en lecture les fichiers de DOSSIER. Toute commande qui écrirait, effacerait ou renommerait
est signalée : le compteur ne doit que lire.
"""
import os
import socket
import ssl
import sys
import time

port, certificat, cle, code, dossier = int(sys.argv[1]), *sys.argv[2:6]
ECRITURES = ("STOR", "STOU", "APPE", "DELE", "RNFR", "RNTO", "MKD", "RMD", "SITE")
MOIS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

contexte = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
contexte.load_cert_chain(certificat, cle)
ecoute = socket.socket()
ecoute.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
ecoute.bind(("127.0.0.1", port))
ecoute.listen(5)
print("fausse carte sur", port, flush=True)


def liste(chemin):
    """Le dossier demandé, comme l'imprimante le liste : droits, taille, date à la minute, nom."""
    local = os.path.realpath(os.path.join(dossier, chemin.lstrip("/")))
    if not (local == os.path.realpath(dossier) or local.startswith(os.path.realpath(dossier) + os.sep)) or not os.path.isdir(local):
        return None
    lignes = []
    for nom in sorted(os.listdir(local)):
        etat = os.stat(os.path.join(local, nom))
        t = time.localtime(etat.st_mtime)
        lignes.append("%s   1 root  root %9d %s %02d %02d:%02d %s" % (
            "-rw-rw-rw-" if os.path.isfile(os.path.join(local, nom)) else "drw-rw-rw-", etat.st_size,
            MOIS[t.tm_mon - 1], t.tm_mday, t.tm_hour, t.tm_min, nom))
    return ("\r\n".join(lignes) + "\r\n").encode("utf-8")


def fichier(chemin):
    """Le fichier servi pour ce chemin, ou None : rien hors du dossier."""
    local = os.path.realpath(os.path.join(dossier, chemin.lstrip("/")))
    return local if local.startswith(os.path.realpath(dossier) + os.sep) and os.path.isfile(local) else None


while True:
    brut, _ = ecoute.accept()
    passif = None
    try:
        liaison = contexte.wrap_socket(brut, server_side=True)
        liaison.settimeout(20)
        lignes = liaison.makefile("r", encoding="utf-8", newline="\r\n")

        def dire(texte):
            liaison.sendall((texte + "\r\n").encode("utf-8"))

        dire("220 fausse carte")
        entre = False
        while True:
            ligne = lignes.readline()
            if not ligne:
                break
            commande, _, argument = ligne.rstrip("\r\n").partition(" ")
            commande = commande.upper()
            if commande == "USER":
                dire("331 ")
            elif commande == "PASS":
                entre = argument == code
                print("PASS, code", "juste" if entre else "faux", flush=True)
                dire("230 " if entre else "530 ")
            elif commande == "QUIT":
                dire("221 ")
                break
            elif not entre:
                dire("530 ")
            elif commande in ("PBSZ", "PROT", "TYPE"):
                dire("200 ")
            elif commande == "SIZE":
                local = fichier(argument)
                dire("213 %d" % os.path.getsize(local) if local else "550 ")
            elif commande == "PASV":
                passif = socket.socket()
                passif.bind(("127.0.0.1", 0))
                passif.listen(1)
                passif.settimeout(20)
                p = passif.getsockname()[1]
                dire("227 Entering Passive Mode (127,0,0,1,%d,%d)" % (p // 256, p % 256))
            elif commande in ("RETR", "LIST"):
                if commande == "RETR":
                    local = fichier(argument)
                    contenu = open(local, "rb").read() if local else None
                else:
                    contenu = liste(argument or "/")
                if contenu is None or not passif:
                    dire("550 ")
                    continue
                print(commande, argument, flush=True)
                dire("150 ")
                donnees = contexte.wrap_socket(passif.accept()[0], server_side=True)
                donnees.settimeout(20)
                donnees.sendall(contenu)
                try:
                    donnees.unwrap()
                    print("fin de transfert propre", flush=True)
                except (OSError, ValueError) as e:
                    print("FIN DE TRANSFERT BRUTALE", e, flush=True)
                donnees.close()
                passif.close()
                passif = None
                dire("226 ")
            elif commande in ECRITURES:
                print("ECRITURE DEMANDEE PAR LE CLIENT", commande, argument[:80], flush=True)
                dire("550 ")
            else:
                dire("502 ")
    except (OSError, ssl.SSLError) as e:
        print("fin de liaison :", e, flush=True)
    finally:
        if passif:
            passif.close()
        brut.close()
