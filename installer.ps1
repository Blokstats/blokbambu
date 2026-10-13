# BlokBambu pour Windows. Installe ou met à jour BlokBambu : copie compteur.py dans le dossier des
# données, le lance sans fenêtre, et le fait démarrer à chaque ouverture de session.
#
#   Installer ou mettre à jour :
#       irm https://raw.githubusercontent.com/Blokstats/blokbambu/main/installer.ps1 | iex
#   Retirer (les données sont gardées) :
#       $env:BLOKBAMBU_ACTION = "retirer"; irm https://raw.githubusercontent.com/Blokstats/blokbambu/main/installer.ps1 | iex
#
# Ce script est exécuté par « iex » dans la fenêtre PowerShell de la personne : il ne doit jamais
# appeler « exit », qui fermerait cette fenêtre.

function Installer-BlokBambu {
    $ErrorActionPreference = "Stop"
    $depot     = "https://raw.githubusercontent.com/Blokstats/blokbambu/main"
    $port      = 47815
    $page      = "http://127.0.0.1:$port/"
    $donnees   = Join-Path $env:APPDATA "BlokBambu"
    $fichier    = Join-Path $donnees "compteur.py"
    $demarrage = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
    $raccourci = Join-Path ([Environment]::GetFolderPath("Desktop")) "BlokBambu.url"
    $fichiers  = "compteur.py", "page.html", "logo.png", "icone.png"

    function Arreter {
        Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
            Where-Object { $_.CommandLine -and $_.CommandLine.Contains($fichier) } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    }

    function Trouver-Python {
        # Rend le chemin d'un vrai Python 3.7 ou plus récent, ou rien.
        $candidats = @()
        foreach ($nom in "py", "python3", "python") {
            $commande = Get-Command $nom -ErrorAction SilentlyContinue
            if ($commande) { $candidats += $commande.Source }
        }
        $candidats += Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe", "$env:ProgramFiles\Python3*\python.exe" -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending | ForEach-Object { $_.FullName }
        foreach ($candidat in $candidats) {
            try {
                # Le « python » de Windows qui renvoie vers le Microsoft Store ne répond rien : il est écarté.
                $reponse = & $candidat -c "import sys; print(sys.executable if sys.version_info >= (3, 7) else '')" 2>$null
                if ($LASTEXITCODE -eq 0 -and $reponse) { return ([string]($reponse | Select-Object -First 1)).Trim() }
            } catch { }
        }
        return $null
    }

    if ($env:BLOKBAMBU_ACTION -eq "retirer") {
        Arreter
        Remove-ItemProperty -Path $demarrage -Name "BlokBambu" -ErrorAction SilentlyContinue
        Remove-Item $raccourci -ErrorAction SilentlyContinue
        Write-Host "BlokBambu est retiré. Vos données restent dans $donnees"
        # Sans cela, une réinstallation dans la même fenêtre retirerait de nouveau BlokBambu.
        Remove-Item Env:BLOKBAMBU_ACTION -ErrorAction SilentlyContinue
        return
    }

    $python = Trouver-Python
    if (-not $python -and (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host "Python n'est pas installé sur cet ordinateur : installation en cours, une minute ou deux..."
        try {
            winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements | Out-Null
        } catch { }
        $python = Trouver-Python
    }
    if (-not $python) {
        Write-Host "Il manque Python sur cet ordinateur. Installez-le depuis https://www.python.org/downloads/"
        Write-Host "(cochez « Add python.exe to PATH »), puis relancez l'installation de BlokBambu."
        return
    }
    # pythonw lance le programme sans fenêtre noire.
    $pythonw = Join-Path (Split-Path $python) "pythonw.exe"
    if (-not (Test-Path $pythonw)) { $pythonw = $python }

    New-Item -ItemType Directory -Force -Path $donnees | Out-Null
    # Les fichiers de BlokBambu : ceux du dossier indiqué par BLOKBAMBU_SOURCE (essais), sinon ceux du
    # dépôt en ligne. Ils ne remplacent les anciens qu'une fois tous arrivés.
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    foreach ($nom in $fichiers) {
        $nouveau = Join-Path $donnees "$nom.nouveau"
        if ($env:BLOKBAMBU_SOURCE) {
            Copy-Item (Join-Path $env:BLOKBAMBU_SOURCE $nom) $nouveau -Force
        } else {
            try {
                Invoke-WebRequest -UseBasicParsing "$depot/$nom" -OutFile $nouveau
            } catch {
                Write-Host "Téléchargement impossible : vérifiez la connexion à Internet."
                return
            }
        }
    }
    Arreter
    foreach ($nom in $fichiers) { Move-Item (Join-Path $donnees "$nom.nouveau") (Join-Path $donnees $nom) -Force }

    # Démarrage à chaque ouverture de session, et lancement tout de suite.
    if (-not (Test-Path $demarrage)) { New-Item -Path $demarrage -Force | Out-Null }
    Set-ItemProperty -Path $demarrage -Name "BlokBambu" -Value ('"{0}" "{1}" --journal' -f $pythonw, $fichier)
    Start-Process -FilePath $pythonw -ArgumentList ('"{0}"' -f $fichier), "--journal" -WindowStyle Hidden

    # Un raccourci vers la page, sur le Bureau.
    Set-Content -Path $raccourci -Value "[InternetShortcut]`r`nURL=$page" -Encoding ASCII

    # La page répond dès que le programme a démarré.
    $pret = $false
    foreach ($essai in 1..40) {
        try {
            Invoke-WebRequest -UseBasicParsing $page -TimeoutSec 2 | Out-Null
            $pret = $true
            break
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    if (-not $pret) {
        throw "BlokBambu n'a pas démarré. Son journal : $donnees\compteur.log"
    }

    Write-Host "BlokBambu est installé."
    Write-Host "  Page      : $page"
    Write-Host "  Raccourci : $raccourci"
    if (-not (Test-Path (Join-Path $env:APPDATA "BambuStudio\BambuStudio.conf"))) {
        Write-Host ""
        Write-Host "Bambu Studio n'a pas été trouvé sur cet ordinateur. Installez-le et connectez-le à votre imprimante :"
        Write-Host "BlokBambu s'en servira tout seul, sans réinstallation."
    }
    if (-not $env:BLOKBAMBU_DISCRET) { Start-Process $page }
}

Installer-BlokBambu
