[CmdletBinding()]
param(
    [switch]$Docker,
    [switch]$NoBuild,
    [switch]$SkipPortCheck,
    [switch]$CheckOnly,
    [switch]$WaitOnExit
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

trap {
    Write-Host ""
    Write-Host "[he-downloader] Startup failed:" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host ""
    if ($WaitOnExit) {
        Read-Host "Press Enter to close this window"
    }
    exit 1
}

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$ComposeFile = Join-Path $Root "compose.yaml"
$EnvFile = Join-Path $Root ".env"
$EnvExample = Join-Path $Root ".env.example"
$GatewayDir = Join-Path $Root "gateway"
$VenvDir = Join-Path $Root ".venv"
$ToolsDir = Join-Path $Root "tools"
$Aria2Version = "1.37.0"
$Aria2ZipUrl = "https://github.com/aria2/aria2/releases/download/release-$Aria2Version/aria2-$Aria2Version-win-64bit-build1.zip"
$Aria2ZipSha256 = "67d015301eef0b612191212d564c5bb0a14b5b9c4796b76454276a4d28d9b288"
$VueUrl = "https://registry.npmmirror.com/vue/3.5.13/files/dist/vue.global.prod.js"
$DockerCmd = $null

Set-Location $Root

function Write-Step([string]$Message) {
    Write-Host "[he-downloader] $Message" -ForegroundColor Cyan
}

function New-RandomSecret {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    } finally {
        $rng.Dispose()
    }
    return [Convert]::ToBase64String($bytes).TrimEnd("=") -replace "\+", "-" -replace "/", "_"
}

function Read-DotEnv([string]$Path) {
    $result = @{}
    if (-not (Test-Path -LiteralPath $Path)) {
        return $result
    }

    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) {
            continue
        }
        if ($trimmed -match "^\s*([^=]+?)\s*=\s*(.*)\s*$") {
            $key = $matches[1].Trim()
            $value = $matches[2].Trim()
            if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            $result[$key] = $value
        }
    }
    return $result
}

function Ensure-EnvFile {
    if (Test-Path -LiteralPath $EnvFile) {
        return
    }
    if (-not (Test-Path -LiteralPath $EnvExample)) {
        throw "Missing .env and .env.example; cannot start."
    }

    $secret = New-RandomSecret
    $content = Get-Content -LiteralPath $EnvExample -Raw
    $content = $content -replace "ARIA2_RPC_SECRET=change-me-to-a-random-secret", "ARIA2_RPC_SECRET=$secret"
    if ($env:OS -eq "Windows_NT") {
        $content = $content -replace "(?m)^MEDIA_DIR=/mnt/hdd\s*$", "MEDIA_DIR=./media"
    }
    Set-Content -LiteralPath $EnvFile -Value $content -Encoding UTF8
    Write-Step "Created .env from .env.example with a random ARIA2_RPC_SECRET."
}

function Ensure-WindowsMediaDir {
    if ($env:OS -ne "Windows_NT") {
        return
    }

    $envMap = Read-DotEnv $EnvFile
    if ($envMap.ContainsKey("MEDIA_DIR") -and [string]$envMap["MEDIA_DIR"] -eq "/mnt/hdd") {
        $content = Get-Content -LiteralPath $EnvFile -Raw
        $content = $content -replace "(?m)^MEDIA_DIR=/mnt/hdd\s*$", "MEDIA_DIR=./media"
        Set-Content -LiteralPath $EnvFile -Value $content -Encoding UTF8
        New-Item -ItemType Directory -Force -Path (Join-Path $Root "media") | Out-Null
        Write-Step "Changed MEDIA_DIR from /mnt/hdd to ./media for Windows local testing."
        return
    }
    if ($envMap.ContainsKey("MEDIA_DIR") -and -not [string]::IsNullOrWhiteSpace([string]$envMap["MEDIA_DIR"])) {
        return
    }

    New-Item -ItemType Directory -Force -Path (Join-Path $Root "media") | Out-Null
    Add-Content -LiteralPath $EnvFile -Value "`r`n# Windows local test media mount; gateway/aria2 can use this as a local media root.`r`nMEDIA_DIR=./media"
    Write-Step "Added MEDIA_DIR=./media to .env for Windows local testing."
}

function Ensure-Aria2Secret {
    $envMap = Read-DotEnv $EnvFile
    $current = if ($envMap.ContainsKey("ARIA2_RPC_SECRET")) { [string]$envMap["ARIA2_RPC_SECRET"] } else { "" }
    if (-not [string]::IsNullOrWhiteSpace($current) -and $current -ne "change-me-to-a-random-secret") {
        return
    }

    $secret = New-RandomSecret
    $content = Get-Content -LiteralPath $EnvFile -Raw
    if ($content -match "(?m)^ARIA2_RPC_SECRET=.*$") {
        $content = $content -replace "(?m)^ARIA2_RPC_SECRET=.*$", "ARIA2_RPC_SECRET=$secret"
        Set-Content -LiteralPath $EnvFile -Value $content -Encoding UTF8
    } else {
        Add-Content -LiteralPath $EnvFile -Value "`r`nARIA2_RPC_SECRET=$secret"
    }
    Write-Step "Wrote a random ARIA2_RPC_SECRET to .env."
}

function Get-EnvValue($EnvMap, [string]$Name, [string]$Default) {
    if ($EnvMap.ContainsKey($Name) -and -not [string]::IsNullOrWhiteSpace([string]$EnvMap[$Name])) {
        return [string]$EnvMap[$Name]
    }
    return $Default
}

function Resolve-ProjectPath([string]$PathValue) {
    if ([string]::IsNullOrWhiteSpace($PathValue)) {
        return $PathValue
    }
    if ([System.IO.Path]::IsPathRooted($PathValue)) {
        return [System.IO.Path]::GetFullPath($PathValue)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $Root $PathValue))
}

function Normalize-ProxyUrl([string]$Proxy) {
    if ([string]::IsNullOrWhiteSpace($Proxy)) {
        return ""
    }
    $value = $Proxy.Trim()
    if ($value -match "^[a-zA-Z][a-zA-Z0-9+.-]*://") {
        return $value
    }
    return "http://$value"
}

function Get-WindowsSystemProxy {
    if ($env:OS -ne "Windows_NT") {
        return ""
    }
    try {
        $settings = Get-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings" -ErrorAction Stop
        if ([int]$settings.ProxyEnable -ne 1 -or [string]::IsNullOrWhiteSpace([string]$settings.ProxyServer)) {
            return ""
        }
        $proxyServer = [string]$settings.ProxyServer
        if ($proxyServer -match "=") {
            $parts = @{}
            foreach ($part in $proxyServer.Split(";")) {
                if ($part -match "^\s*([^=]+)=(.+)\s*$") {
                    $parts[$matches[1].Trim().ToLowerInvariant()] = $matches[2].Trim()
                }
            }
            foreach ($key in @("http", "https", "socks", "socks5")) {
                if ($parts.ContainsKey($key)) {
                    return Normalize-ProxyUrl ([string]$parts[$key])
                }
            }
            return ""
        }
        return Normalize-ProxyUrl $proxyServer
    } catch {
        return ""
    }
}

function Test-TcpPortAvailable([int]$Port) {
    $listener = $null
    try {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Any, $Port)
        $listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        if ($null -ne $listener) {
            $listener.Stop()
        }
    }
}

function Test-UdpPortAvailable([int]$Port) {
    $socket = $null
    try {
        $socket = [System.Net.Sockets.UdpClient]::new($Port)
        return $true
    } catch {
        return $false
    } finally {
        if ($null -ne $socket) {
            $socket.Dispose()
        }
    }
}

function Get-PortOwner([int]$Port, [string]$Protocol) {
    try {
        if ($Protocol -eq "tcp") {
            $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($conn) {
                $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
                if ($proc) {
                    return "$($proc.ProcessName) (pid $($proc.Id))"
                }
                return "pid $($conn.OwningProcess)"
            }
        } else {
            $endpoint = Get-NetUDPEndpoint -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($endpoint) {
                $proc = Get-Process -Id $endpoint.OwningProcess -ErrorAction SilentlyContinue
                if ($proc) {
                    return "$($proc.ProcessName) (pid $($proc.Id))"
                }
                return "pid $($endpoint.OwningProcess)"
            }
        }
    } catch {
        return "unknown process"
    }
    return "unknown process"
}

function Assert-PortAvailable([string]$Name, [int]$Port, [string]$Protocol, [bool]$SkipBecauseOwned) {
    if ($SkipBecauseOwned) {
        Write-Step "$Name $Protocol/$Port is already owned by this stack; attaching logs."
        return
    }

    $available = if ($Protocol -eq "tcp") {
        Test-TcpPortAvailable $Port
    } else {
        Test-UdpPortAvailable $Port
    }

    if (-not $available) {
        $owner = Get-PortOwner $Port $Protocol
        throw "$Name port $Protocol/$Port is already in use by $owner. Stop that process or change the port in .env."
    }
    Write-Step "$Name $Protocol/$Port is available."
}

function Resolve-DockerCommand {
    $cmd = Get-Command docker -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }

    $candidates = @(
        "C:\Program Files\Docker\Docker\resources\bin\docker.exe",
        "$env:LOCALAPPDATA\Docker\resources\bin\docker.exe"
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return $candidate
        }
    }
    return $null
}

function Require-Docker {
    $script:DockerCmd = Resolve-DockerCommand
    if (-not $script:DockerCmd) {
        throw "Docker CLI was not found. Install/start Docker Desktop first."
    }
    & $script:DockerCmd compose version *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "'docker compose' is not available. Update Docker Desktop or install the Compose plugin."
    }
}

function Find-Python {
    $venvPython = Join-Path $VenvDir "Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython) {
        return $venvPython
    }

    $candidates = @(
        "C:\Users\25768\AppData\Local\Programs\Python\Python312\python.exe"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }

    $cmd = Get-Command py -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }
    throw "Python was not found. Install Python 3.12 or add python.exe to PATH."
}

function Ensure-Venv {
    $venvPython = Join-Path $VenvDir "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $venvPython)) {
        $basePython = Find-Python
        Write-Step "Creating local Python virtual environment."
        if ((Split-Path -Leaf $basePython) -eq "py.exe") {
            & $basePython -3.12 -m venv $VenvDir
        } else {
            & $basePython -m venv $VenvDir
        }
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to create .venv."
        }
    }

    Write-Step "Installing/updating gateway Python dependencies."
    & $venvPython -m pip install -q -r (Join-Path $GatewayDir "requirements.txt") | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install gateway Python dependencies."
    }
    return $venvPython
}

function Ensure-VueRuntime {
    $vuePath = Join-Path $GatewayDir "app\static\vue.global.prod.js"
    if (Test-Path -LiteralPath $vuePath) {
        return
    }
    Write-Step "Downloading Vue runtime for the local panel."
    Invoke-WebRequest -Uri $VueUrl -OutFile $vuePath
}

function Find-Aria2 {
    $local = Join-Path $ToolsDir "aria2c.exe"
    if (Test-Path -LiteralPath $local) {
        return $local
    }

    $cmd = Get-Command aria2c -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }
    return $null
}

function Ensure-Aria2 {
    $aria2 = Find-Aria2
    if ($aria2) {
        return $aria2
    }

    if ($env:OS -ne "Windows_NT") {
        throw "aria2c was not found. Install aria2 and make aria2c available on PATH."
    }

    Write-Step "aria2c not found; downloading aria2 $Aria2Version for Windows."
    New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null
    $zipPath = Join-Path $ToolsDir "aria2-$Aria2Version-win64.zip"
    Invoke-WebRequest -Uri $Aria2ZipUrl -OutFile $zipPath
    $actualHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $Aria2ZipSha256) {
        Remove-Item -LiteralPath $zipPath -Force
        throw "Downloaded aria2 checksum mismatch."
    }
    $extractDir = Join-Path $ToolsDir "aria2-$Aria2Version"
    if (Test-Path -LiteralPath $extractDir) {
        Remove-Item -LiteralPath $extractDir -Recurse -Force
    }
    Expand-Archive -LiteralPath $zipPath -DestinationPath $extractDir -Force
    $exe = Get-ChildItem -LiteralPath $extractDir -Recurse -Filter aria2c.exe | Select-Object -First 1
    if (-not $exe) {
        throw "Downloaded aria2 archive did not contain aria2c.exe."
    }
    Copy-Item -LiteralPath $exe.FullName -Destination (Join-Path $ToolsDir "aria2c.exe") -Force
    return (Join-Path $ToolsDir "aria2c.exe")
}

function Start-LocalProcess([string]$Name, [string]$FilePath, [string[]]$Arguments, [string]$WorkDir) {
    Write-Step "Starting $Name."
    $process = Start-Process -FilePath $FilePath -ArgumentList $Arguments -WorkingDirectory $WorkDir -NoNewWindow -PassThru
    Start-Sleep -Milliseconds 700
    if ($process.HasExited) {
        throw "$Name exited immediately with code $($process.ExitCode)."
    }
    return $process
}

function Stop-LocalProcess($Process, [string]$Name) {
    if ($null -eq $Process -or $Process.HasExited) {
        return
    }
    Write-Step "Stopping $Name."
    try {
        $Process.CloseMainWindow() | Out-Null
        Start-Sleep -Milliseconds 800
    } catch {
    }
    if (-not $Process.HasExited) {
        Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
    }
}

function Start-DockerMode($EnvMap, [int]$GatewayPort, [int]$Aria2RpcPort) {
    Require-Docker
    $runningContainers = @(& $DockerCmd ps --format "{{.Names}}" 2>$null)
    $gatewayRunning = $runningContainers -contains "he_downloader_gateway"
    $aria2Running = $runningContainers -contains "he_downloader_aria2"

    if (-not $SkipPortCheck) {
        Assert-PortAvailable "gateway" $GatewayPort "tcp" $gatewayRunning
        Assert-PortAvailable "aria2 RPC" $Aria2RpcPort "tcp" $aria2Running
        Assert-PortAvailable "aria2 BitTorrent" 6888 "tcp" $aria2Running
        Assert-PortAvailable "aria2 BitTorrent" 6888 "udp" $aria2Running
    } else {
        Write-Step "Skipping port checks."
    }

    if ($CheckOnly) {
        Write-Step "Docker check-only mode completed."
        return 0
    }

    $composeArgs = @("compose", "--env-file", $EnvFile, "-f", $ComposeFile, "up")
    if (-not $NoBuild) {
        $composeArgs += "--build"
    }

    Write-Step "Starting Docker Compose in this window. Press Ctrl+C to stop."
    Write-Step "Gateway panel: http://localhost:$GatewayPort"

    & $DockerCmd @composeArgs
    return $LASTEXITCODE
}

function Start-LocalMode($EnvMap, [int]$GatewayPort, [int]$Aria2RpcPort) {
    if ($env:OS -ne "Windows_NT") {
        throw "Local mode is intended for Windows. Use -Docker on Linux."
    }

    if (-not $SkipPortCheck) {
        Assert-PortAvailable "gateway" $GatewayPort "tcp" $false
        Assert-PortAvailable "aria2 RPC" $Aria2RpcPort "tcp" $false
        Assert-PortAvailable "aria2 BitTorrent" 6888 "tcp" $false
        Assert-PortAvailable "aria2 BitTorrent" 6888 "udp" $false
    } else {
        Write-Step "Skipping port checks."
    }

    if ($CheckOnly) {
        Ensure-Aria2 | Out-Null
        Ensure-VueRuntime
        Ensure-Venv | Out-Null
        Write-Step "Local check-only mode completed."
        return 0
    }

    $aria2 = Ensure-Aria2
    Ensure-VueRuntime
    $python = Ensure-Venv

    $downloadDir = Resolve-ProjectPath (Get-EnvValue $EnvMap "DOWNLOAD_DIR" ".\downloads")
    $mediaDir = Resolve-ProjectPath (Get-EnvValue $EnvMap "MEDIA_DIR" ".\media")
    $dataDir = Join-Path $Root "data\gateway"
    $dbPath = Join-Path $dataDir "gateway.db"
    New-Item -ItemType Directory -Force -Path $downloadDir, $mediaDir, $dataDir | Out-Null

    $aria2Secret = Get-EnvValue $EnvMap "ARIA2_RPC_SECRET" ""
    $gatewayToken = Get-EnvValue $EnvMap "GATEWAY_API_TOKEN" ""
    $heManagerUrl = Get-EnvValue $EnvMap "HE_MANAGER_URL" ""
    $heManagerToken = Get-EnvValue $EnvMap "HE_MANAGER_TOKEN" ""
    $aria2Proxy = Get-EnvValue $EnvMap "ARIA2_ALL_PROXY" ""

    $env:ARIA2_RPC_URL = "http://127.0.0.1:$Aria2RpcPort/jsonrpc"
    $env:ARIA2_RPC_SECRET = $aria2Secret
    $env:GATEWAY_API_TOKEN = $gatewayToken
    $env:DOWNLOAD_DIR = $downloadDir
    $env:GATEWAY_DB_PATH = $dbPath
    $env:HE_MANAGER_URL = $heManagerUrl
    $env:HE_MANAGER_TOKEN = $heManagerToken
    $env:ARIA2_ALL_PROXY = $aria2Proxy

    $aria2Args = @(
        "--enable-rpc",
        "--rpc-listen-all=false",
        "--rpc-allow-origin-all=true",
        "--rpc-listen-port=$Aria2RpcPort",
        "--rpc-secret=$aria2Secret",
        "--dir=$downloadDir",
        "--continue=true",
        "--max-concurrent-downloads=3",
        "--disk-cache=64M",
        "--auto-file-renaming=false",
        "--allow-overwrite=false",
        "--listen-port=6888",
        "--dht-listen-port=6888",
        "--seed-time=0",
        "--bt-stop-timeout=120"
    )
    $gatewayArgs = @(
        "-m", "uvicorn", "app.main:app",
        "--host", "127.0.0.1",
        "--port", "$GatewayPort"
    )

    $aria2Proc = $null
    $gatewayProc = $null
    try {
        $aria2Proc = Start-LocalProcess "aria2" $aria2 $aria2Args $Root
        $gatewayProc = Start-LocalProcess "gateway" $python $gatewayArgs $GatewayDir
        Write-Step "Gateway panel: http://localhost:$GatewayPort"
        Write-Step "Local services are running in this window. Press Ctrl+C to stop."

        while ($true) {
            if ($aria2Proc.HasExited) {
                throw "aria2 exited with code $($aria2Proc.ExitCode)."
            }
            if ($gatewayProc.HasExited) {
                throw "gateway exited with code $($gatewayProc.ExitCode)."
            }
            Start-Sleep -Seconds 1
        }
    } finally {
        Stop-LocalProcess $gatewayProc "gateway"
        Stop-LocalProcess $aria2Proc "aria2"
    }
}

Ensure-EnvFile
Ensure-WindowsMediaDir
Ensure-Aria2Secret
$envMap = Read-DotEnv $EnvFile
$gatewayPort = [int](Get-EnvValue $envMap "GATEWAY_PORT" "8011")
$aria2RpcPort = [int](Get-EnvValue $envMap "ARIA2_RPC_PORT" "6800")

if ($Docker) {
    $exitCode = Start-DockerMode $envMap $gatewayPort $aria2RpcPort
} else {
    $exitCode = Start-LocalMode $envMap $gatewayPort $aria2RpcPort
}

if ($WaitOnExit -and $exitCode -ne 0) {
    Write-Host ""
    Write-Host "[he-downloader] Exited with code $exitCode." -ForegroundColor Yellow
    Read-Host "Press Enter to close this window"
}
exit $exitCode
