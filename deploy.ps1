$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$dir = Join-Path $env:USERPROFILE 'Downloads\sevimli-kassa-SERVER\sevimli-kassa-SERVER'
if (-not (Test-Path (Join-Path $dir 'manage.py'))) {
    Write-Host 'SERVER papkasi topilmadi:' -ForegroundColor Red
    Write-Host $dir
    return
}
Set-Location $dir

# 1. Git bor-yoqligini tekshiramiz, bolmasa vaqtincha yuklab olamiz
$git = 'git'
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    $pg = Join-Path $dir 'PortableGit'
    $gitexe = Join-Path $pg 'bin\git.exe'
    if (-not (Test-Path $gitexe)) {
        Write-Host 'Git yuklab olinmoqda (~50 MB), kuting...' -ForegroundColor Yellow
        $rel = Invoke-RestMethod 'https://api.github.com/repos/git-for-windows/git/releases/latest' -Headers @{ 'User-Agent' = 'sevimli' }
        $asset = $rel.assets | Where-Object { $_.name -match 'PortableGit-.*-64-bit\.7z\.exe$' } | Select-Object -First 1
        if (-not $asset) { Write-Host 'Git yuklab olinmadi.' -ForegroundColor Red; return }
        $out = Join-Path $dir 'pgit.exe'
        Invoke-WebRequest $asset.browser_download_url -OutFile $out
        Write-Host 'Ochilmoqda...' -ForegroundColor Yellow
        Start-Process -FilePath $out -ArgumentList "-o`"$pg`"", '-y' -Wait -NoNewWindow
        Remove-Item $out -ErrorAction SilentlyContinue
    }
    $git = $gitexe
}
Write-Host ('Git: ' + (& $git --version))

# 2. GitHub kirishni brauzer orqali soraydigan qilamiz
& $git config --global credential.helper manager | Out-Null
& $git config --global credential.https://github.com.provider github | Out-Null

# 3. Repo tayyorlab, yuboramiz
if (-not (Test-Path '.git')) { & $git init | Out-Null }
& $git config user.email 'deploy@sevimli.uz'
& $git config user.name 'Sevimli Deploy'
& $git add -A
& $git commit -m 'Sevimli Kassa server' 2>$null | Out-Null
& $git branch -M main
& $git remote remove origin 2>$null | Out-Null
& $git remote add origin 'https://github.com/130395mq-dev/sevimli-kassa.git'
Write-Host ''
Write-Host 'Yuklanmoqda... GitHub kirish oynasi chiqsa - tasdiqlang.' -ForegroundColor Cyan
& $git push -u origin main --force
if ($LASTEXITCODE -ne 0) {
    Write-Host ''
    Write-Host 'YUKLANMADI. Yuqoridagi yozuvni suratga oling.' -ForegroundColor Red
    return
}
Write-Host ''
Write-Host '================================' -ForegroundColor Green
Write-Host 'TAYYOR - kod GitHub ga yuklandi!' -ForegroundColor Green
Write-Host '================================' -ForegroundColor Green
