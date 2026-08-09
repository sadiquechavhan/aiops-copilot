# =============================================================================
# Downloads the OpenTelemetry Java agent.
#
# The agent is mounted into all three service containers read-only rather than
# baked into their images: one 25 MB download instead of three, and the version
# can be changed without rebuilding anything.
#
# The cost of that choice is this bootstrap step. Session 10 has to solve it
# properly for "clone and make up" to work on a stranger's machine.
#
# Usage:  .\tools\fetch-agent.ps1
# =============================================================================

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'

# Read the pinned version out of .env so there is exactly one place to change it.
$repoRoot = Split-Path -Parent $PSScriptRoot
$envFile  = Join-Path $repoRoot '.env'

if (-not (Test-Path $envFile)) { throw "Cannot find .env at $envFile" }

$version = (Get-Content $envFile |
            Where-Object { $_ -match '^\s*OTEL_AGENT_VERSION\s*=' } |
            ForEach-Object { ($_ -split '=', 2)[1].Trim() } |
            Select-Object -First 1)

if ([string]::IsNullOrWhiteSpace($version)) {
    throw "OTEL_AGENT_VERSION is not set in .env"
}

$targetDir = Join-Path $repoRoot 'otel\agent'
$target    = Join-Path $targetDir 'opentelemetry-javaagent.jar'
$url       = "https://github.com/open-telemetry/opentelemetry-java-instrumentation/releases/download/v$version/opentelemetry-javaagent.jar"

if (-not (Test-Path $targetDir)) { New-Item -ItemType Directory -Path $targetDir | Out-Null }

Write-Host "Downloading OpenTelemetry Java agent $version"
Write-Host "  from $url"
Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $target

$sizeMb = [math]::Round((Get-Item $target).Length / 1MB, 1)
Write-Host "Wrote $target ($sizeMb MB)"

if ($sizeMb -lt 5) {
    throw "Downloaded file is only $sizeMb MB - that is almost certainly an error page, not the agent."
}
