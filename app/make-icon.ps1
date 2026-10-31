param([string]$Bat, [string]$Dir)
$name = '日迹 1.0'
$Dir = $Dir.Trim().Trim('"').TrimEnd('\')
$desktop = [Environment]::GetFolderPath('Desktop')
$sh = New-Object -ComObject WScript.Shell
$ico = Join-Path $Dir 'app\riji.ico'
foreach ($f in Get-ChildItem $desktop -Filter *.lnk -ErrorAction SilentlyContinue) {
  try {
    $t = $sh.CreateShortcut($f.FullName).TargetPath
    if ($t -and ($t -ieq $Bat)) {
      $c = $sh.CreateShortcut($f.FullName)
      $c.IconLocation = $ico + ',0'
      $c.Description = ''
      $c.Save()
      exit 0
    }
  } catch {}
}
$lnk = Join-Path $desktop ($name + '.lnk')
$s = $sh.CreateShortcut($lnk)
$s.TargetPath = $Bat
$s.WorkingDirectory = $Dir
$s.IconLocation = $ico + ',0'
$s.Description = ''
$s.Save()
