<#
.SYNOPSIS
    Manage a local, self-contained PostgreSQL cluster for this project.

.DESCRIPTION
    Runs PostgreSQL from the binaries-only distribution as an ordinary user
    process: no installer, no administrator rights, and no Windows service.
    That matters because a portfolio project should be reproducible on a
    locked-down machine, which is exactly where this one was built.

    The cluster listens on localhost only, on a non-default port, so it cannot
    collide with any PostgreSQL a reader may already have installed.

.PARAMETER Command
    setup    initdb a fresh cluster and configure it
    start    start the server
    stop     stop the server
    status   report whether it is running
    psql     open an interactive shell against the project database

.EXAMPLE
    .\scripts\pg.ps1 setup
    .\scripts\pg.ps1 start
    .\scripts\pg.ps1 psql
#>
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("setup", "start", "stop", "status", "psql")]
    [string]$Command
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $RepoRoot ".env"

function Get-EnvValue($key, $default) {
    if (Test-Path $EnvFile) {
        $line = Select-String -Path $EnvFile -Pattern "^$key=" | Select-Object -First 1
        if ($line) { return ($line.Line -split "=", 2)[1].Trim() }
    }
    return $default
}

$WorkDir = (Get-EnvValue "COVEO_WORK_DIR" "D:/coveo-sigir/work") -replace "/work$", ""
$PgBin   = Join-Path $WorkDir "pgsql\bin"
$PgData  = Join-Path $WorkDir "pgdata"
$Port    = Get-EnvValue "PGPORT" "5433"
$DbName  = Get-EnvValue "PGDATABASE" "coveo_analytics"
$DbUser  = Get-EnvValue "PGUSER" "postgres"

if (-not (Test-Path "$PgBin\pg_ctl.exe")) {
    Write-Error @"
PostgreSQL binaries not found at $PgBin

Download the binaries-only ZIP (no installer, no admin rights needed) from
https://www.enterprisedb.com/download-postgresql-binaries and extract it so
that $PgBin\pg_ctl.exe exists. See docs/POSTGRES_SETUP.md.
"@
}

switch ($Command) {

    "setup" {
        if (Test-Path (Join-Path $PgData "PG_VERSION")) {
            Write-Output "Cluster already exists at $PgData - nothing to do."
            break
        }
        # Never bake a credential into a tracked file. .env is gitignored.
        $pw = Get-EnvValue "PGPASSWORD" ""
        if (-not $pw) {
            $pw = -join ((48..57) + (97..122) | Get-Random -Count 24 | ForEach-Object { [char]$_ })
            Write-Output "Generated a local password and recorded it in .env"
            if (Test-Path $EnvFile) {
                $c = Get-Content $EnvFile
                if ($c -match "^PGPASSWORD=") { $c = $c -replace "^PGPASSWORD=.*", "PGPASSWORD=$pw" }
                else { $c += "PGPASSWORD=$pw" }
                Set-Content $EnvFile $c -Encoding ascii
            }
        }

        $pwFile = Join-Path $env:TEMP "pgpw_$([guid]::NewGuid()).txt"
        Set-Content -Path $pwFile -Value $pw -NoNewline -Encoding ascii
        try {
            & "$PgBin\initdb.exe" -D $PgData -U $DbUser --pwfile=$pwFile -E UTF8 --locale=C
        } finally {
            Remove-Item $pwFile -Force -ErrorAction SilentlyContinue
        }

        # localhost only, non-default port, password required over TCP.
        $conf = Get-Content "$PgData\postgresql.conf"
        $conf = $conf -replace "^#?port\s*=.*", "port = $Port"
        $conf = $conf -replace "^#?listen_addresses\s*=.*", "listen_addresses = 'localhost'"
        Set-Content "$PgData\postgresql.conf" $conf -Encoding ascii

        $hba = Get-Content "$PgData\pg_hba.conf"
        $hba = $hba -replace "^(host\s+all\s+all\s+127\.0\.0\.1/32\s+)\w+", '$1scram-sha-256'
        $hba = $hba -replace "^(host\s+all\s+all\s+::1/128\s+)\w+", '$1scram-sha-256'
        Set-Content "$PgData\pg_hba.conf" $hba -Encoding ascii

        Write-Output "Cluster created at $PgData (port $Port, localhost only)."
        Write-Output "Next:  .\scripts\pg.ps1 start"
    }

    "start" {
        & "$PgBin\pg_ctl.exe" -D $PgData -l (Join-Path $PgData "server.log") -o "-p $Port" start
        Start-Sleep -Seconds 3
        & "$PgBin\createdb.exe" -h localhost -p $Port -U $DbUser $DbName 2>$null
        Write-Output "Ready on localhost:$Port/$DbName"
    }

    "stop"   { & "$PgBin\pg_ctl.exe" -D $PgData stop }

    "status" { & "$PgBin\pg_ctl.exe" -D $PgData status }

    "psql" {
        $env:PGPASSWORD = Get-EnvValue "PGPASSWORD" ""
        & "$PgBin\psql.exe" -h localhost -p $Port -U $DbUser -d $DbName
    }
}
