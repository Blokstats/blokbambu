"""Fausse imprimante pour les essais : parle MQTT sur TLS comme une Bambu et publie ce qu'on lui dicte.

    python faux_imprimante.py PORT CERTIFICAT CLE SERIE CODE FICHIER_DICTE

Chaque ligne ajoutée au fichier dicté est publiée telle quelle sur device/SERIE/report.
Tout ce que le client publierait est signalé : le compteur ne doit rien envoyer.
"""
import os
import socket
import ssl
import struct
import sys

port, certificat, cle, serie, code, dictee = int(sys.argv[1]), *sys.argv[2:7]


def paquet(entete, corps):
    taille, longueur = len(corps), b""
    while True:
        taille, reste = divmod(taille, 128)
        longueur += bytes([reste | (128 if taille else 0)])
        if not taille:
            return bytes([entete]) + longueur + corps


def lire(liaison, tampon):
    """Rend (en-tête, corps), ou None si rien de complet n'est arrivé."""
    while True:
        taille, decalage = 0, 0
        for i in range(1, min(len(tampon), 5)):
            taille |= (tampon[i] & 127) << decalage
            decalage += 7
            if not tampon[i] & 128:
                if len(tampon) >= i + 1 + taille:
                    recu = (tampon[0], bytes(tampon[i + 1:i + 1 + taille]))
                    del tampon[:i + 1 + taille]
                    return recu
                break
        try:
            bloc = liaison.recv(65536)
        except (socket.timeout, ssl.SSLWantReadError):
            return None
        if not bloc:
            raise ConnectionError("client parti")
        tampon += bloc


contexte = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
contexte.load_cert_chain(certificat, cle)
ecoute = socket.socket()
ecoute.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
ecoute.bind(("127.0.0.1", port))
ecoute.listen(5)
print("fausse imprimante sur", port, flush=True)
publiees = 0
while True:
    brut, _ = ecoute.accept()
    try:
        liaison = contexte.wrap_socket(brut, server_side=True)
        liaison.settimeout(0.3)
        tampon, abonne = bytearray(), False
        while True:
            recu = lire(liaison, tampon)
            if recu:
                entete, corps = recu
                if entete == 0x10:
                    presente = corps.split(b"bblp")[-1][2:].decode()
                    print("CONNECT, code", "juste" if presente == code else "faux", flush=True)
                    liaison.sendall(bytes([0x20, 2, 0, 0 if presente == code else 5]))
                    if presente != code:
                        break
                elif entete == 0x82:
                    liaison.sendall(bytes([0x90, 3]) + corps[:2] + b"\x00")
                    abonne = True
                    print("SUBSCRIBE", flush=True)
                elif entete == 0xC0:
                    liaison.sendall(b"\xD0\x00")
                elif entete & 0xF0 == 0x30:
                    print("COMMANDE PUBLIEE PAR LE CLIENT", corps[:80], flush=True)
            if abonne and os.path.exists(dictee):
                with open(dictee, encoding="utf-8") as f:
                    lignes = f.read().splitlines()
                for ligne in lignes[publiees:]:
                    sujet = ("device/%s/report" % serie).encode()
                    liaison.sendall(paquet(0x30, struct.pack("!H", len(sujet)) + sujet + ligne.encode()))
                publiees = len(lignes)
    except (OSError, ssl.SSLError, ConnectionError) as e:
        print("fin de liaison :", e, flush=True)
    finally:
        brut.close()
