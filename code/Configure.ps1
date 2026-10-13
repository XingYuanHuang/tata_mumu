param([string]$EngineRoot='', [int]$Instance=-1, [string]$PythonPath='')
$ErrorActionPreference='Stop'
Set-Location -LiteralPath $PSScriptRoot
$data=Join-Path $PSScriptRoot 'data'
New-Item -ItemType Directory -Path $data -Force | Out-Null
if(!$EngineRoot){
 $roots=@(Get-Process -ErrorAction SilentlyContinue | ForEach-Object {
  try { $candidate=Split-Path -Parent $_.Path
   for($i=0;$i -lt 7 -and $candidate;$i++){
    if(Test-Path -LiteralPath (Join-Path $candidate 'vms')){ $candidate;break }
    $candidate=Split-Path -Parent $candidate
   }
  }catch{}
 } | Select-Object -Unique)
 if($roots.Count -eq 1){$EngineRoot=$roots[0]}
 else{$EngineRoot=Read-Host '请输入 MuMu engine 目录（该目录下应有 vms 文件夹）'}
}
$EngineRoot=(Resolve-Path -LiteralPath $EngineRoot).Path
$choices=@(Get-ChildItem -LiteralPath (Join-Path $EngineRoot 'vms') -Directory | ForEach-Object {
 $file=Join-Path $_.FullName 'configs\vm_config.json'
 if((Test-Path -LiteralPath $file) -and $_.Name -match '-(\d+)$'){
  $idx=[int]$Matches[1]
  $cfg=Get-Content -LiteralPath $file -Raw | ConvertFrom-Json
  $port=$cfg.vm.nat.port_forward.adb.host_port
  if($port){[pscustomobject]@{name=$_.Name;index=$idx;port=$port}}
 }
})
if(!$choices.Count){throw '没有找到可用的 MuMu 实例配置，请检查 engine 目录。'}
$choices | Format-Table name,index,port
if($Instance -lt 0){if($choices.Count -eq 1){$Instance=$choices[0].index}else{$Instance=[int](Read-Host '请输入使用的实例 index')}}
$selected=@($choices | Where-Object index -eq $Instance)
if($selected.Count -ne 1){throw '实例编号无效'}
$config=@{root=$EngineRoot;instance=$Instance;serial=('127.0.0.1:'+$selected[0].port);vm=$selected[0].name}
$config | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $data 'mumu-ipc.json') -Encoding UTF8
if(!$PythonPath){
 $command=Get-Command python.exe -ErrorAction SilentlyContinue
 if($command){$PythonPath=$command.Source}
 else{
  $launcher=Get-Command py.exe -ErrorAction SilentlyContinue
  if($launcher){$PythonPath=(& $launcher.Source -3 -c 'import sys;print(sys.executable)' | Select-Object -Last 1)}
 }
}
if(!$PythonPath){$PythonPath=Read-Host '请输入 Python 3.10–3.13 64位的 python.exe 完整路径'}
$PythonPath=(Resolve-Path -LiteralPath $PythonPath).Path
& $PythonPath -c 'import sys,struct;assert (3,10)<=sys.version_info[:2]<=(3,13);assert struct.calcsize("P")==8'
if($LASTEXITCODE -ne 0){throw '需要 Python 3.10–3.13 64位版本'}
& $PythonPath -c 'import cv2,numpy'
if($LASTEXITCODE -ne 0){
 $venv=Join-Path $PSScriptRoot '.venv'
 & $PythonPath -m venv $venv
 if($LASTEXITCODE -ne 0){throw '创建本地 Python 环境失败'}
 $PythonPath=Join-Path $venv 'Scripts\python.exe'
 & $PythonPath -m pip install --only-binary=:all: 'numpy==2.2.6' 'opencv-python==4.12.0.88'
 if($LASTEXITCODE -ne 0){throw '安装钓鱼依赖失败，请检查网络和错误信息'}
}
@{python=$PythonPath} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $data 'fishing-runtime.json') -Encoding UTF8
Write-Host '配置完成。请关闭模拟器多应用保活，打开钓鱼页面，双击 Start.bat。'
