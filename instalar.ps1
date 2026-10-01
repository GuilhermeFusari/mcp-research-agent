# Instalador do Agente de Pesquisa (Windows). Uso: duplo clique em instalar.bat

param(
    [switch]$SemIndexar,
    [switch]$SemRegistrar
)

$ErrorActionPreference = "Stop"
$Projeto = $PSScriptRoot
$Venv    = Join-Path $Projeto ".venv"
$Py      = Join-Path $Venv "Scripts\python.exe"

function Passo($texto) { Write-Host "`n==> $texto" -ForegroundColor Cyan }
function Falha($texto) { Write-Host "`nERRO: $texto" -ForegroundColor Red; exit 1 }

Passo "1/6 Procurando Python 3.12 ou mais novo"
$PythonBase = $null
foreach ($cmd in @("py -3.13", "py -3.12", "py -3.14", "python")) {
    $exe   = $cmd.Split(" ")[0]
    $extra = @($cmd.Split(" ") | Select-Object -Skip 1)
    try {
        $ok = & $exe @extra -c "import sys; print(sys.version_info >= (3, 12))" 2>$null
        if ($ok -eq "True") { $PythonBase = @($exe) + $extra; break }
    } catch { }
}
if (-not $PythonBase) {
    Falha "Python 3.12+ não encontrado. Instale em https://www.python.org/downloads/ (marque 'Add python.exe to PATH') e rode de novo."
}
Write-Host "Usando: $($PythonBase -join ' ')"

Passo "2/6 Criando ambiente virtual e instalando bibliotecas (a 1ª vez demora)"
if (-not (Test-Path $Py)) {
    $extra = @($PythonBase | Select-Object -Skip 1)
    & $PythonBase[0] @extra -m venv $Venv
}
& $Py -m pip install --upgrade pip --quiet
& $Py -m pip install -r (Join-Path $Projeto "requirements.txt")
if ($LASTEXITCODE -ne 0) { Falha "pip install falhou (veja as mensagens acima)." }

Passo "3/6 Criando config.toml com as suas pastas"
$Config = Join-Path $Projeto "config.toml"
if (Test-Path $Config) {
    Write-Host "config.toml já existe; mantendo o seu."
} else {
    $desktop   = [Environment]::GetFolderPath("Desktop")
    $documents = [Environment]::GetFolderPath("MyDocuments")
    $downloads = (New-Object -ComObject Shell.Application).NameSpace("shell:Downloads").Self.Path

    $texto = Get-Content (Join-Path $Projeto "config.exemplo.toml") -Raw -Encoding UTF8
    $texto = $texto.Replace("'~\Desktop'", "'$desktop'").Replace("'~\Downloads'", "'$downloads'").Replace("'~\Documents'", "'$documents'")
    [IO.File]::WriteAllText($Config, $texto, (New-Object Text.UTF8Encoding $false))
    Write-Host "Pastas: $desktop | $downloads | $documents"
    Write-Host "Edite config.toml se quiser mudar pastas ou exclusões."
}

Passo "4/6 Preparando o banco vetorial (Qdrant)"
$Exe = Join-Path $Projeto "bin\qdrant.exe"
if (Test-Path $Exe) {
    Write-Host "qdrant.exe já baixado."
} else {
    $versao = "v1.19.1"
    $hashEsperado = "9b6f69bd85f6abed4bc13f943099f55c6ffd55f5dd90388635320d8fbb569eb0"
    $url = "https://github.com/qdrant/qdrant/releases/download/$versao/qdrant-x86_64-pc-windows-msvc.zip"
    $zip = Join-Path $env:TEMP "qdrant-$versao.zip"

    Write-Host "Baixando Qdrant $versao (~30 MB)..."
    $ProgressPreference = "SilentlyContinue"
    Invoke-WebRequest $url -OutFile $zip -UseBasicParsing
    $hash = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
    if ($hash -ne $hashEsperado) {
        Remove-Item $zip
        Falha "O arquivo baixado não confere com o hash oficial. Não vou executá-lo."
    }
    Expand-Archive $zip (Join-Path $Projeto "bin") -Force
    Remove-Item $zip
    Write-Host "Hash conferido; qdrant.exe instalado em bin\."
}
& $Py -c "import rag; rag.garantir_qdrant(rag.carregar_config()); print('Qdrant rodando.')"
if ($LASTEXITCODE -ne 0) { Falha "Não consegui iniciar o Qdrant. Veja logs\qdrant.log." }

Passo "5/6 Baixando o modelo de embeddings (~1 GB, só na 1ª vez)"
& $Py -c "import rag; rag.criar_embeddings(rag.carregar_config()).embed_query('teste'); print('Modelo pronto.')"
if ($LASTEXITCODE -ne 0) { Falha "Não consegui baixar o modelo (sem internet?)." }

Passo "6/6 Registrando no Claude Desktop"
if ($SemRegistrar) {
    Write-Host "Pulado (-SemRegistrar)."
} else {
    & $Py (Join-Path $Projeto "registrar_claude.py")
    if ($LASTEXITCODE -ne 0) { Falha "Registro no Claude Desktop falhou." }
}

Write-Host "`nInstalação concluída!" -ForegroundColor Green
if (-not $SemIndexar) {
    $r = Read-Host "Indexar seus PDFs agora? [s/N]"
    if ($r -eq "s") { & $Py (Join-Path $Projeto "indexar.py") }
    else { Write-Host "Quando quiser: .venv\Scripts\python.exe indexar.py" }
}
Write-Host "Depois reinicie o Claude Desktop (ícone da bandeja > Sair) e pergunte algo."
