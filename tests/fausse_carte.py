"""Fausse carte mémoire d'imprimante pour les essais : FTP chiffré dès la connexion, comme sur une Bambu.

    python fausse_carte.py PORT CERTIFICAT CLE CODE DOSSIER

Sert en lecture les fichiers de DOSSIER. Toute commande qui écrirait, effacerait ou renommerait
est signalée : le compteur ne doit que lire.
"""
import os
import socket
import ssl
import sys

port, certificat, cle, code, dossier = int(sys.argv[1]), *sys.argv[2:6]
ECRITURES = ("STOR", "STOU", "APPE", "DELE", "RNFR", "RNTO", "MKD", "RMD", "SITE")

contexte = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
contexte.load_cert_chain(certificat, cle)
ecoute = socket.socket()
ecoute.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
ecoute.bind(("127.0.0.1", port))
ecoute.listen(5)
print("fausse carte sur", port, flush=True)


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
            elif commande == "RETR":
                local = fichier(argument)
                if not local or not passif:
                    dire("550 ")
                    continue
                print("RETR", argument, flush=True)
                dire("150 ")
                donnees = contexte.wrap_socket(passif.accept()[0], server_side=True)
                donnees.settimeout(20)
                with open(local, "rb") as f:
                    donnees.sendall(f.read())
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
