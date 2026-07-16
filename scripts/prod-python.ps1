param(
    [string]$Target = "root@47.76.243.147",
    [string]$Workdir = "/opt/OdAIly",
    [string]$Python = "",
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
    throw "No Python script received on stdin."
}

$encoded = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($script))
$quotedWorkdir = Quote-BashSingle $Workdir
$quotedPython = Quote-BashSingle $Python
$remote = @"
cd $quotedWorkdir
if [ -n $quotedPython ]; then
  PY=$quotedPython
elif [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
else
  PY=python3
fi
printf '%s' '$encoded' | base64 -d | "`$PY" -
"@

& ssh -o BatchMode=yes -o ConnectTimeout=$ConnectTimeout $Target $remote
exit $LASTEXITCODE
