param([string]$Bat, [string]$Dir)
$name = '日迹 1.0'
$desktop = [Environment]::GetFolderPath('Desktop')
$sh = New-Object -ComObject WScript.Shell
foreach ($f in Get-ChildItem $desktop -Filter *.lnk -ErrorAction SilentlyContinue) {
  try {
    $t = $sh.CreateShortcut($f.FullName).TargetPath
    if ($t -and ($t -ieq $Bat)) { exit 0 }
  } catch {}
}
$lnk = Join-Path $desktop ($name + '.lnk')
$s = $sh.CreateShortcut($lnk)
$s.TargetPath = $Bat
$s.WorkingDirectory = $Dir
$s.IconLocation = (Join-Path $Dir 'app\riji.ico') + ',0'
$s.Save()
