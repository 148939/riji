param([string]$Bat, [string]$Dir)
$name = [char]0x65E5 + [char]0x8FF9
$desktop = [Environment]::GetFolderPath('Desktop')
$lnk = Join-Path $desktop ($name + '.lnk')
if (Test-Path $lnk) { exit 0 }
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
$s.TargetPath = $Bat
$s.WorkingDirectory = $Dir
$s.IconLocation = (Join-Path $Dir 'app\riji.ico') + ',0'
$s.Save()
