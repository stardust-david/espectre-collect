#Requires -Version 5.1
<#
.SYNOPSIS
    ESPectre 一键流程：设备自检 -> 批量采集 -> 质量校验 -> 模型训练

.DESCRIPTION
    本脚本把原本需要手动敲的多条命令串成一条流程，方便在 Windows 上运行。
    真正执行采集的是同目录下的 collect_batch.py；本脚本只负责参数收集、
    流程编排与结果汇总。

    如需以非交互方式运行，可显式传入 -Repo / -Target / -Room。

.PARAMETER Repo
    ESPectre 仓库根目录。不传则交互式询问，默认 $HOME\espectre。

.PARAMETER Target
    设备 IP 或主机名（例如 192.168.1.50）。不传则交互式询问。

.PARAMETER Room
    房间名，写入 dataset_info.json 的 environment。不传则交互式询问。

.PARAMETER Rounds
    每个标签采集条数，默认 10。

.PARAMETER Duration
    每条录音秒数，默认 60。

.PARAMETER Pps
    采集流量发包率，默认 100。应与设备 csi_target_pps 一致，不要随意改动。

.PARAMETER Role
    写入 dataset_role，默认 train。可选 exclude / train / selection / holdout。

.PARAMETER SelfCheckOnly
    只做设备自检，不采集。

.PARAMETER NoTrain
    采集完成后跳过训练步骤。

.PARAMETER ExportModel
    训练时直接走生产流程（通过门禁则导出权重），不询问。

.EXAMPLE
    .\run_all.ps1 -Repo $HOME\espectre -Target 192.168.1.50 -Room bedroom

.EXAMPLE
    .\run_all.ps1 -SelfCheckOnly
#>
[CmdletBinding()]
param(
    [string]$Repo,
    [string]$Target,
    [string]$Room,
    [int]$Rounds = 10,
    [int]$Duration = 60,
    [int]$Pps = 100,
    [ValidateSet('exclude', 'train', 'selection', 'holdout')]
    [string]$Role = 'train',
    [switch]$SelfCheckOnly,
    [switch]$NoTrain,
    [switch]$ExportModel
)

$ErrorActionPreference = 'Stop'

# 让 PowerShell 按 UTF-8 读取外部程序（Python）的输出，避免中文乱码
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch { }

# --------------------------------------------------------------------------
# 输出辅助
# --------------------------------------------------------------------------
function Write-Head([string]$Text) {
    Write-Host ''
    Write-Host ('=' * 68) -ForegroundColor Cyan
    Write-Host $Text -ForegroundColor Cyan
    Write-Host ('=' * 68) -ForegroundColor Cyan
}

function Write-Step([string]$Text) {
    Write-Host ''
    Write-Host ('-' * 68) -ForegroundColor DarkCyan
    Write-Host $Text -ForegroundColor DarkCyan
    Write-Host ('-' * 68) -ForegroundColor DarkCyan
}

function Write-Ok([string]$Text)   { Write-Host "  [ok] $Text" -ForegroundColor Green }
function Write-Note([string]$Text) { Write-Host "  [!]  $Text" -ForegroundColor Yellow }
function Write-Bad([string]$Text)  { Write-Host "  [x]  $Text" -ForegroundColor Red }

function Stop-WithMessage([string]$Text, [int]$Code) {
    Write-Bad $Text
    Write-Host ''
    [void](Read-Host '按回车退出')
    exit $Code
}

function Confirm-Continue([string]$Prompt) {
    $answer = Read-Host $Prompt
    if ([string]::IsNullOrWhiteSpace($answer)) { return $true }
    return ($answer -match '^[Yy]')
}

# --------------------------------------------------------------------------
# 定位脚本与仓库
# --------------------------------------------------------------------------
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Collector = Join-Path $ScriptDir 'collect_batch.py'

Write-Head 'ESPectre 一键流程：自检 -> 采集 -> 校验 -> 训练'

if (-not (Test-Path $Collector)) {
    Stop-WithMessage "找不到 collect_batch.py：$Collector`n      请把 run_all.ps1 / run_all.bat / collect_batch.py 放在同一目录。" 2
}
Write-Ok "采集脚本：$Collector"

# --------------------------------------------------------------------------
# 收集参数
# --------------------------------------------------------------------------
if (-not $Repo) {
    $repoDefault = Join-Path $HOME 'espectre'
    $ans = Read-Host "ESPectre 仓库路径（回车用默认 $repoDefault）"
    if ([string]::IsNullOrWhiteSpace($ans)) { $Repo = $repoDefault } else { $Repo = $ans.Trim() }
}
if (-not (Test-Path $Repo)) {
    Stop-WithMessage "仓库路径不存在：$Repo" 2
}
$Repo = $Repo.TrimEnd([char[]]@('\', '/'))

if (-not $Target) {
    $Target = (Read-Host 'ESP32 的 IP 地址（例如 192.168.1.50）').Trim()
}
if ([string]::IsNullOrWhiteSpace($Target)) {
    Stop-WithMessage '必须提供设备 IP。' 2
}

if (-not $Room) {
    $Room = (Read-Host '房间名（例如 bedroom，同一房间请始终用同一个名字）').Trim()
}
if ([string]::IsNullOrWhiteSpace($Room)) { $Room = 'room' }

# 选择 Python 解释器：优先仓库虚拟环境
$venvPyWin = Join-Path $Repo '.venv\Scripts\python.exe'
$venvPyNix = Join-Path $Repo '.venv\bin\python'
if (Test-Path $venvPyWin)     { $Python = $venvPyWin }
elseif (Test-Path $venvPyNix) { $Python = $venvPyNix }
else                          { $Python = 'python' }

$pyVer = $null
try {
    $pyVer = & $Python -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>&1
}
catch {
    $pyVer = $null
}
if (-not $pyVer -or $LASTEXITCODE -ne 0) {
    Write-Bad "无法运行 Python：$Python"
    Write-Bad '请先安装 Python，或确认仓库下已建好 .venv（python -m venv .venv）。'
    if (-not (Test-Path $venvPyWin)) {
        Write-Note '注意：训练步骤需要 Python 3.14。'
    }
    Write-Host ''
    [void](Read-Host '按回车退出')
    exit 2
}

# --------------------------------------------------------------------------
# 参数摘要
# --------------------------------------------------------------------------
$trainRc = $null
$estMinutes = [math]::Ceiling($Rounds * 3 * ($Duration + 25) / 60.0)

Write-Step '运行参数'
Write-Host "  仓库       : $Repo"
Write-Host "  Python     : $Python  ($pyVer)"
Write-Host "  设备       : $Target"
Write-Host "  房间       : $Room"
Write-Host "  每条时长   : $Duration 秒"
Write-Host "  每标签条数 : $Rounds"
Write-Host "  发包率     : $Pps pps"
Write-Host "  元数据角色 : $Role"
Write-Host "  预计耗时   : 约 $estMinutes 分钟（三个标签，含人工准备时间）"
Write-Host ''

if (-not (Confirm-Continue '确认开始自检吗？(Y/n)')) {
    Write-Host '已取消。'
    exit 0
}

# --------------------------------------------------------------------------
# 步骤 1：设备自检
# --------------------------------------------------------------------------
Write-Step '[1/3] 设备自检'

& $Python $Collector --repo $Repo --target $Target --room $Room --self-check
$rc = $LASTEXITCODE

if ($rc -ne 0) {
    Write-Bad "自检未通过（退出码 $rc）。"
    if ($rc -eq 3) {
        Write-Bad '设备不可用或不满足采集条件，常见原因：'
        Write-Bad '  - 设备 IP 写错，或电脑与设备不在同一局域网（路由器开了 AP 隔离）'
        Write-Bad '  - Windows 防火墙拦截了 Python 的网络访问'
        Write-Bad '  - 固件是 micro 前端：它不提供 /csi 端点，需要重刷为 Native 固件'
        Write-Bad '  - capabilities.features.csi 为 false'
        Write-Bad ''
        Write-Bad '先在 PowerShell 里执行 ping <设备IP> 排查网络，再重新运行。'
    }
    elseif ($rc -eq 2) {
        Write-Bad '仓库或脚本路径有问题，请检查上面的输出。'
    }
    Write-Host ''
    [void](Read-Host '按回车退出')
    exit $rc
}
Write-Ok '自检通过，设备可以采集。'

if ($SelfCheckOnly) {
    Write-Host ''
    Write-Ok '自检模式结束（-SelfCheckOnly）。'
    exit 0
}

# --------------------------------------------------------------------------
# 步骤 2：批量采集（内部含质量校验）
# --------------------------------------------------------------------------
Write-Step '[2/3] 批量采集'

Write-Note '接下来按标签依次采集，每条之间需要你按回车确认。'
Write-Note 'empty 需要人离开房间并保持安静；motion 需要人在房间里走动。'
Write-Note '采集期间 Monitor 页面上的运动曲线会暂停，这是官方设计。'
Write-Host ''

if (-not (Confirm-Continue '确认开始采集吗？(Y/n)')) {
    Write-Host '已取消。'
    exit 0
}

& $Python $Collector --repo $Repo --target $Target --room $Room --rounds $Rounds --duration $Duration --pps $Pps --role $Role
$rc = $LASTEXITCODE

if ($rc -eq 0) {
    Write-Ok '采集与质量校验全部通过。'
}
elseif ($rc -eq 4) {
    Write-Note '有录音未通过质量校验（占用率低于 70%）。失败的文件已保留用于诊断。'
    Write-Note '注意：不要靠降低 -Pps 解决。优先检查蓝牙干扰、设备位置、mesh 漫游。'
}
elseif ($rc -eq 1) {
    Write-Note '采集过程中有部分录音失败，请查看上面的日志。'
}
else {
    Write-Note "采集流程返回退出码 $rc。"
}

if ($NoTrain) {
    Write-Host ''
    Write-Ok '已跳过训练（-NoTrain）。'
    exit $rc
}

# --------------------------------------------------------------------------
# 步骤 3：训练
# --------------------------------------------------------------------------
Write-Step '[3/3] 训练模型'

# 检查训练依赖
$depsOk = $false
Push-Location $Repo
try {
    & $Python -c "import sklearn, numpy, scipy" 2>&1 | Out-Null
    $depsOk = ($LASTEXITCODE -eq 0)
}
catch {
    $depsOk = $false
}
finally {
    Pop-Location
}

if (-not $depsOk) {
    Write-Note '训练依赖尚未安装。请先执行：'
    Write-Host '      pip install -r requirements-ml.txt' -ForegroundColor White
    Write-Note '训练需要 Python 3.14（低于该版本可能无法安装依赖或训练失败）。'
    Write-Host ''
    if (-not (Confirm-Continue '仍然尝试运行训练吗？(y/N)')) {
        Write-Host '已跳过训练。'
        exit $rc
    }
}
else {
    Write-Ok '训练依赖已就绪。'
}

# 先看会用到哪些录音
Write-Host ''
Write-Host '  $ python tools/train_ml_model.py --info' -ForegroundColor DarkGray
Push-Location $Repo
try {
    & $Python 'tools/train_ml_model.py' --info
}
finally {
    Pop-Location
}

Write-Host ''
Write-Note "只有 dataset_role = train 的录音才会参与拟合；selection / holdout / exclude 都不参与。"
Write-Note "当前本次新增录音的角色是：$Role"

# 选择训练模式
if ($ExportModel) {
    $mode = '2'
}
else {
    Write-Host ''
    Write-Host '  [1] 只训练并查看指标，不覆盖固件权重（推荐先跑这个）' -ForegroundColor White
    Write-Host '  [2] 生产训练：通过门禁则导出权重，会覆盖 ml_weights.h / ml_weights.py' -ForegroundColor White
    $mode = Read-Host '选择 (回车默认 1)'
    if ([string]::IsNullOrWhiteSpace($mode)) { $mode = '1' }
}

if ($mode -eq '2') {
    Write-Host ''
    Write-Note '生产训练。门禁（recall / 误报率）不通过时不会导出权重。'
    Write-Host '  $ python tools/train_ml_model.py --augment' -ForegroundColor DarkGray
    Push-Location $Repo
    try {
        & $Python 'tools/train_ml_model.py' --augment
        $trainRc = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
    if ($trainRc -eq 0) {
        Write-Ok '训练完成。若通过门禁，权重已导出到 src/cpp/core/ml_weights.h 和 tools/lib/ml_weights.py。'
    }
    else {
        Write-Note "训练返回退出码 $trainRc，通常是门禁未通过。权重文件未被修改。"
        Write-Note '数据量偏少时（例如只有一个房间、只有 train 角色）门禁容易不过，这属于正常现象。'
    }
}
else {
    Write-Host ''
    Write-Note '只训练不导出。这样不会动到固件权重文件，可以放心反复跑。'
    Write-Host '  $ python tools/train_ml_model.py --augment --no-export' -ForegroundColor DarkGray
    Push-Location $Repo
    try {
        & $Python 'tools/train_ml_model.py' --augment --no-export
        $trainRc = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
    if ($trainRc -eq 0) {
        Write-Ok '训练完成，请查看上面的交叉验证指标。'
    }
    else {
        Write-Note "训练返回退出码 $trainRc。"
    }
}

# --------------------------------------------------------------------------
# 汇总
# --------------------------------------------------------------------------
Write-Head '流程结束'
Write-Host "  采集退出码 : $rc （0 = 全部通过，4 = 质量校验有失败项）"
Write-Host "  训练退出码 : $trainRc"
Write-Host ''
Write-Host '  数据位置   : ' -NoNewline; Write-Host "$Repo\data\<标签>\" -ForegroundColor White
Write-Host '  数据目录   : ' -NoNewline; Write-Host "$Repo\data\dataset_info.json" -ForegroundColor White
Write-Host '  采集日志   : ' -NoNewline; Write-Host "$ScriptDir\logs\" -ForegroundColor White
Write-Host ''
Write-Host '  后续常用命令（在仓库目录下执行）：' -ForegroundColor White
Write-Host '    python tools\validate_dataset_quality.py    # 再次做质量校验'
Write-Host '    python tools\train_ml_model.py --info       # 查看训练会用哪些录音'
Write-Host '    python tools\train_ml_model.py --augment    # 生产训练并导出权重'
Write-Host ''

exit 0
