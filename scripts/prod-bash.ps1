param(
    [string]$Target = "root@47.76.243.147",
    [string]$Workdir = "/opt/OdAIly",
    [int]$ConnectTimeout = 15
)

$ErrorActionPreference = "Stop"
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

function Quote-BashSingle([string]$Value) {
    return "'" + $Value.Replace("'", "'\''") + "'"
}

$script = ($input | ForEach-Object { [string]$_ }) -join [Environment]::NewLine
if ([string]::IsNullOrWhiteSpace($script)) {
    throw "No bash script received on stdin."
}

$payload = "set -euo pipefail`n" + $script
$encoded = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($payload))
$quotedWorkdir = Quote-BashSingle $Workdir
$remote = "cd $quotedWorkdir && printf '%s' '$encoded' | base64 -d | bash"

& ssh -o BatchMode=yes -o ConnectTimeout=$ConnectTimeout $Target $remote
exit $LASTEXITCODE
