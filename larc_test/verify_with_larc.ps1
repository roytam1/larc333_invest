# verify_with_larc.ps1
#
# Runs the real LARC.EXE (under msdos.exe) against the generated test
# archives and checks that
#   * the listing shows the expected members / method codes / CRC
#   * the extraction produces byte identical files
#   * no "CRC Error" is reported
#
# usage:  powershell -File verify_with_larc.ps1 [-Keep]

param([switch]$Keep)

$ErrorActionPreference = "Stop"
$root     = Split-Path -Parent $MyInvocation.MyCommand.Path
$arcDir   = Join-Path $root "archives"
$repo     = Split-Path -Parent $root
$larc     = Join-Path $repo "LARC.EXE"
$ovr      = Join-Path $repo "LARC7E.OVR"
$msdos    = Join-Path $repo "msdos.exe"
$sandbox  = Join-Path $env:TEMP ("larc_verify_" + [guid]::NewGuid().ToString("N").Substring(0,8))

if (-not (Test-Path -LiteralPath $msdos)) { throw "msdos.exe not found in $repo" }

New-Item -ItemType Directory -Path $sandbox | Out-Null

function Invoke-Larc {
    param([string[]]$LarcArgs, [string]$Cwd, [int]$TimeoutSec = 30)
    # MS-DOS Player maps the current Windows directory to C:.
    # LArc can fall back to its interactive mode (and then waits for a key),
    # so the call runs in a job with a timeout; stdin gets a few newlines and
    # the output is redirected to a file (no pipe deadlock).
    $tag = [guid]::NewGuid().ToString("N")
    $outFile = Join-Path $Cwd "larc_out_$tag.txt"
    $inFile  = Join-Path $Cwd "larc_in_$tag.txt"
    Set-Content -LiteralPath $inFile -Value "`r`n`r`n`r`n`r`n`r`n" -NoNewline -Encoding ASCII
    $job = Start-Job -ScriptBlock {
        param($msdos, $Cwd, $inFile, $outFile, $argLine)
        Set-Location -LiteralPath $Cwd
        cmd /c "`"$msdos`" $argLine < `"$inFile`" > `"$outFile`" 2>&1"
    } -ArgumentList $msdos, $Cwd, $inFile, $outFile, ("LARC.EXE " + ($LarcArgs -join " "))
    if (Wait-Job $job -Timeout $TimeoutSec) { Receive-Job $job | Out-Null }
    else { Stop-Job $job }
    Remove-Job $job -Force -ErrorAction SilentlyContinue
    $text = ""
    if (Test-Path -LiteralPath $outFile) { $text = Get-Content -LiteralPath $outFile -Raw }
    Remove-Item -LiteralPath $inFile, $outFile -ErrorAction SilentlyContinue
    if ($text) { ($text -split "`r?`n") | Where-Object { $_ -notmatch '^System\.Management' -and $_.Trim() -ne '' } }
}

# expected payload of every member of every archive
$expected = @{}
foreach ($p in @("hello.txt", "repeat.bin", "pseudo.bin")) {
    $expected[$p.ToUpper()] = [System.IO.File]::ReadAllBytes((Join-Path (Join-Path $arcDir "payload") $p))
}

# mode: "extract"  -> the members must be unpacked byte exactly
#       "list"     -> the members must show up in the listing
#       "reject"   -> LArc must refuse the archive
$tests = @(
    @{ arc = "t_lz4.lzs";         mode = "extract"; expect = @("HELLO.TXT","REPEAT.BIN") },
    @{ arc = "t_lz5.lzs";         mode = "extract"; expect = @("HELLO.TXT","REPEAT.BIN","PSEUDO.BIN") },
    @{ arc = "t_lz8.lzs";         mode = "extract"; expect = @("HELLO.TXT","REPEAT.BIN") },
    @{ arc = "t_native.lzs"; mode = "extract"; expect = @("HELLO.TXT","REPEAT.BIN") },
    @{ arc = "t_lzs.lzs";         mode = "extract"; expect = @("HELLO.TXT","REPEAT.BIN","PSEUDO.BIN") },
    @{ arc = "t_lz123.lzs"; mode = "extract"; expect = @("HELLO.TXT","REPEAT.BIN","PSEUDO.BIN") },
    @{ arc = "t_lz7.lzs";         mode = "reject";  expect = @() },
    @{ arc = "t_mixed.lzs";       mode = "extract"; expect = @("HELLO.TXT","REPEAT.BIN","PSEUDO.BIN") },
    @{ arc = "t_badhdr.lzs";  mode = "reject";  expect = @() },
    @{ arc = "t_badcrc.lzs";     mode = "crcerr";  expect = @("HELLO.TXT") }
)

$fail = 0
foreach ($t in $tests) {
    $arcPath = Join-Path $arcDir $t.arc
    if (-not (Test-Path -LiteralPath $arcPath)) { Write-Host "MISSING $($t.arc)"; $fail++; continue }
    $dir = Join-Path $sandbox ([System.IO.Path]::GetFileNameWithoutExtension($t.arc))
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
    Copy-Item -LiteralPath $arcPath -Destination $dir
    Copy-Item -LiteralPath $larc, $ovr -Destination $dir

    $list = Invoke-Larc @("L", $t.arc) $dir
    $extr = ""
    if ($t.mode -ne "list") {
        # /o = overwrite, without it LArc asks "Over write ?" and waits
        $extr = Invoke-Larc @("E", "/o", $t.arc) $dir
    }
    $all  = ($list + $extr) -join "`n"

    $problems = @()
    if ($all -match 'CRC Error' -and $t.mode -ne "crcerr") { $problems += "CRC Error reported" }

    switch ($t.mode) {
        "extract" {
            foreach ($f in $t.expect) {
                $out = Join-Path $dir $f
                if (-not (Test-Path -LiteralPath $out)) { $problems += "$f not extracted"; continue }
                $got = [System.IO.File]::ReadAllBytes($out)
                $exp = $expected[$f]
                if ($got.Length -ne $exp.Length) { $problems += "$f length $($got.Length) != $($exp.Length)"; continue }
                $same = $true
                for ($i = 0; $i -lt $exp.Length; $i++) { if ($got[$i] -ne $exp[$i]) { $same = $false; break } }
                if (-not $same) { $problems += "$f content differs" }
            }
        }
        "list" {
            foreach ($f in $t.expect) {
                if (-not ($list -match [regex]::Escape($f))) { $problems += "$f not listed" }
            }
        }
        "reject" {
            if ($list -match 'HELLO.TXT') { $problems += "archive was accepted although it should be refused" }
        }
        "crcerr" {
            if (-not ($list -match 'HELLO.TXT')) { $problems += "member is not listed" }
            if (-not ($extr -match 'CRC Error')) { $problems += "no CRC Error reported" }
        }
    }

    $status = if ($problems.Count -eq 0) { "OK  " } else { "FAIL" }
    if ($problems.Count -gt 0) { $fail++ }
    Write-Host ("{0} {1,-22} {2}" -f $status, $t.arc, ($problems -join '; '))
    if ($problems.Count -gt 0) {
        Write-Host "     --- LArc output ---"
        ($all -split "`n") | ForEach-Object { Write-Host "     $_" }
    }
}

Write-Host ""
if ($fail -eq 0) { Write-Host "all archives behave as expected" } else { Write-Host "$fail archive(s) failed" }
if (-not $Keep) { Remove-Item -Recurse -Force $sandbox -ErrorAction SilentlyContinue }
else { Write-Host "sandbox kept: $sandbox" }
exit $fail
