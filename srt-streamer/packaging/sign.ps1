# Signs every not-yet-signed .exe/.dll/.pyd under the given files or folders with Microsoft Trusted
# Signing, then verifies them. Used by the CI workflow (see SIGNING.md).
#   pwsh packaging/sign.ps1 vendor dist-stats
# Needs AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, TS_ENDPOINT, TS_ACCOUNT, TS_PROFILE.
param([Parameter(Mandatory = $true, ValueFromRemainingArguments = $true)][string[]]$Paths)
$ErrorActionPreference = "Stop"

function Get-Unsigned([string[]]$where) {
    Get-ChildItem $where -Recurse -File -Include *.exe, *.dll, *.pyd |
        Where-Object { (Get-AuthenticodeSignature $_.FullName).Status -ne 'Valid' }
}

$files = @(Get-Unsigned $Paths)
if ($files.Count -eq 0) { Write-Host "Nothing to sign in $Paths"; return }
Write-Host "Signing $($files.Count) file(s):"; $files.FullName | ForEach-Object { Write-Host "  $_" }

if (-not (Get-Module -ListAvailable -Name TrustedSigning)) {
    Install-Module -Name TrustedSigning -Force -Scope CurrentUser -Repository PSGallery
}
$list = Join-Path $env:RUNNER_TEMP "to-sign-$([guid]::NewGuid()).txt"
$files.FullName | Set-Content -Path $list -Encoding utf8
Invoke-TrustedSigning `
    -Endpoint $env:TS_ENDPOINT -CodeSigningAccountName $env:TS_ACCOUNT -CertificateProfileName $env:TS_PROFILE `
    -FilesCatalog $list `
    -FileDigest SHA256 -TimestampRfc3161 "http://timestamp.acs.microsoft.com" -TimestampDigest SHA256

$bad = @(Get-Unsigned $Paths)
if ($bad.Count) { $bad.FullName; throw "$($bad.Count) file(s) are still unsigned" }
$files | ForEach-Object {
    $s = Get-AuthenticodeSignature $_.FullName
    Write-Host ("{0}: {1} ({2})" -f $_.Name, $s.Status, $s.SignerCertificate.Subject)
}
