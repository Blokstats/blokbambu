// BlokBambu — la fenêtre d'alerte sur Mac, aux couleurs de la page.
//
//   alerte "texte" ["rappel"] [secondes]
//
// Code de sortie : 0 = « Ouvrir la page », 1 = « Plus tard » (ou fermeture au bout des secondes
// données, pour les essais). L'installeur compile ce fichier sur place ; s'il n'y parvient pas,
// le compteur montre la fenêtre du système à la place.

import AppKit
import CoreText

let arguments = CommandLine.arguments
let texte = arguments.count > 1 ? arguments[1] : ""
let rappel = arguments.count > 2 ? arguments[2] : ""
let duree = arguments.count > 3 ? Double(arguments[3]) ?? 0 : 0

func couleur(_ hex: UInt32, _ alpha: CGFloat = 1) -> NSColor {
    NSColor(srgbRed: CGFloat((hex >> 16) & 0xFF) / 255, green: CGFloat((hex >> 8) & 0xFF) / 255,
            blue: CGFloat(hex & 0xFF) / 255, alpha: alpha)
}

let jaune = couleur(0xFFB400), panneau = couleur(0x111111), clair = couleur(0xE8E4DC), discret = couleur(0xB0AEA8)
let largeur: CGFloat = 430, marge: CGFloat = 24

/// La police des titres de la page, posée à côté de ce programme ; sinon la plus proche du système.
func policeTitre(_ taille: CGFloat) -> NSFont {
    let fichier = URL(fileURLWithPath: CommandLine.arguments[0]).deletingLastPathComponent().appendingPathComponent("police.woff2")
    if let descripteurs = CTFontManagerCreateFontDescriptorsFromURL(fichier as CFURL) as? [CTFontDescriptor],
       let premier = descripteurs.first {
        CTFontManagerRegisterFontsForURL(fichier as CFURL, .process, nil)
        return CTFontCreateWithFontDescriptor(premier, taille, nil) as NSFont
    }
    if #available(macOS 13.0, *) { return NSFont.systemFont(ofSize: taille, weight: .black, width: .expanded) }
    return NSFont.systemFont(ofSize: taille, weight: .black)
}

func etiquette(_ chaine: NSAttributedString) -> NSTextField {
    let champ = NSTextField(wrappingLabelWithString: "")
    champ.attributedStringValue = chaine
    champ.preferredMaxLayoutWidth = largeur - 2 * marge
    champ.isSelectable = false
    return champ
}

func texteStyle(_ chaine: String, _ police: NSFont, _ teinte: NSColor, interligne: CGFloat = 3) -> NSAttributedString {
    let paragraphe = NSMutableParagraphStyle()
    paragraphe.lineSpacing = interligne
    return NSAttributedString(string: chaine, attributes: [.font: police, .foregroundColor: teinte, .paragraphStyle: paragraphe])
}

final class Bouton: NSButton {
    init(_ titre: String, plein: Bool, cible: AnyObject, action: Selector) {
        super.init(frame: .zero)
        self.target = cible
        self.action = action
        isBordered = false
        wantsLayer = true
        layer?.cornerRadius = 7
        layer?.backgroundColor = (plein ? jaune : NSColor.clear).cgColor
        layer?.borderWidth = plein ? 0 : 1
        layer?.borderColor = couleur(0xFFFFFF, 0.13).cgColor
        attributedTitle = NSAttributedString(string: titre, attributes: [
            .font: NSFont.systemFont(ofSize: 13, weight: plein ? .bold : .semibold),
            .foregroundColor: plein ? NSColor.black : discret])
        translatesAutoresizingMaskIntoConstraints = false
        heightAnchor.constraint(equalToConstant: 34).isActive = true
        widthAnchor.constraint(equalToConstant: attributedTitle.size().width + 30).isActive = true
    }
    required init?(coder: NSCoder) { nil }
}

final class Fenetre: NSPanel {
    override var canBecomeKey: Bool { true }
}

final class Delegue: NSObject, NSApplicationDelegate {
    var fenetre: Fenetre!

    func applicationDidFinishLaunching(_ note: Notification) {
        let nom = NSMutableAttributedString(string: "BLOK", attributes: [.font: policeTitre(13), .foregroundColor: NSColor.white, .kern: 0.5])
        nom.append(NSAttributedString(string: "BAMBU", attributes: [.font: policeTitre(13), .foregroundColor: jaune, .kern: 0.5]))

        let lignes = texte.components(separatedBy: "\n")
        let pile = NSStackView()
        pile.orientation = .vertical
        pile.alignment = .leading
        pile.spacing = 8
        pile.edgeInsets = NSEdgeInsets(top: marge, left: marge, bottom: marge - 4, right: marge)
        pile.addArrangedSubview(etiquette(nom))
        pile.setCustomSpacing(16, after: pile.arrangedSubviews.last!)
        pile.addArrangedSubview(etiquette(texteStyle(lignes.first ?? "", .systemFont(ofSize: 15, weight: .semibold), .white)))
        if lignes.count > 1 {
            pile.addArrangedSubview(etiquette(texteStyle(lignes.dropFirst().joined(separator: "\n"), .systemFont(ofSize: 13), clair, interligne: 4)))
        }
        if !rappel.isEmpty {
            pile.addArrangedSubview(etiquette(texteStyle(rappel, .systemFont(ofSize: 12), discret)))
        }
        let boutons = NSStackView(views: [Bouton("Plus tard", plein: false, cible: self, action: #selector(plusTard)),
                                          Bouton("Ouvrir la page", plein: true, cible: self, action: #selector(ouvrir))])
        boutons.orientation = .horizontal
        boutons.spacing = 10
        let rangee = NSStackView(views: [NSView(), boutons])
        rangee.orientation = .horizontal
        rangee.translatesAutoresizingMaskIntoConstraints = false
        rangee.widthAnchor.constraint(equalToConstant: largeur - 2 * marge).isActive = true
        pile.setCustomSpacing(20, after: pile.arrangedSubviews.last!)
        pile.addArrangedSubview(rangee)

        let fond = NSView()
        fond.wantsLayer = true
        fond.layer?.backgroundColor = panneau.cgColor
        fond.layer?.cornerRadius = 14
        fond.layer?.borderWidth = 1
        fond.layer?.borderColor = couleur(0xFFFFFF, 0.13).cgColor
        pile.translatesAutoresizingMaskIntoConstraints = false
        fond.addSubview(pile)
        NSLayoutConstraint.activate([
            pile.topAnchor.constraint(equalTo: fond.topAnchor), pile.bottomAnchor.constraint(equalTo: fond.bottomAnchor),
            pile.leadingAnchor.constraint(equalTo: fond.leadingAnchor), pile.trailingAnchor.constraint(equalTo: fond.trailingAnchor),
            fond.widthAnchor.constraint(equalToConstant: largeur)])

        fenetre = Fenetre(contentRect: NSRect(x: 0, y: 0, width: largeur, height: 200), styleMask: [.borderless],
                          backing: .buffered, defer: false)
        fenetre.isOpaque = false
        fenetre.backgroundColor = .clear
        fenetre.hasShadow = true
        fenetre.level = .floating
        fenetre.isMovableByWindowBackground = true
        fenetre.contentView = fond
        fond.layoutSubtreeIfNeeded()
        fenetre.setContentSize(fond.fittingSize)
        if let ecran = NSScreen.main?.visibleFrame {  // au tiers haut de l'écran, comme une alerte
            fenetre.setFrameOrigin(NSPoint(x: ecran.midX - largeur / 2,
                                           y: ecran.minY + ecran.height * 0.62 - fenetre.frame.height / 2))
        }
        fenetre.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        if duree > 0 { DispatchQueue.main.asyncAfter(deadline: .now() + duree) { exit(1) } }
    }

    @objc func ouvrir() { exit(0) }
    @objc func plusTard() { exit(1) }
}

// Entrée : ouvrir la page ; Échap : plus tard.
NSEvent.addLocalMonitorForEvents(matching: .keyDown) { evenement in
    if evenement.keyCode == 36 || evenement.keyCode == 76 { exit(0) }
    if evenement.keyCode == 53 { exit(1) }
    return evenement
}

let application = NSApplication.shared
let delegue = Delegue()
application.delegate = delegue
application.setActivationPolicy(.accessory)
application.run()
