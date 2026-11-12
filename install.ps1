<#
把 VideoCut skill 链接到各个 agent 的 skill 目录（目录联接，不复制；以后 git pull 更新后各 agent 直接用到新版本）。

默认装到：
  ~\.agents\skills\videocut   Codex，以及其它遵循 Agent Skills 开放标准、读这个目录的 agent
  ~\.claude\skills\videocut   Claude Code

用法：
  .\install.ps1                              # 装到默认位置
  .\install.ps1 -Target D:\x\skills          # 另外装到某个 agent 自己的 skill 目录（可写多个，逗号分隔）
  .\install.ps1 -NoShortcut                  # 不在桌面放「VideoCut 工作台」快捷方式
  .\install.ps1 -Uninstall                   # 删掉这些链接和桌面快捷方式（不动仓库里的文件和你的数据）

如果提示"禁止运行脚本"，用：powershell -ExecutionPolicy Bypass -File .\install.ps1
#>
param(
    [string[]]$Target = @(),
    [switch]$NoShortcut,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$src = Join-Path $PSScriptRoot 'skills\videocut'
if (-not (Test-Path (Join-Path $src 'SKILL.md'))) {
    Write-Error "找不到 $src\SKILL.md，请在仓库根目录运行这个脚本。"
}

$dirs = @(
    (Join-Path $env:USERPROFILE '.agents\skills'),
    (Join-Path $env:USERPROFILE '.claude\skills')
) + $Target

foreach ($d in $dirs) {
    $link = Join-Path $d 'videocut'
    $item = Get-Item -LiteralPath $link -Force -ErrorAction SilentlyContinue
    $isLink = $item -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)

    if ($item -and -not $isLink) {
        Write-Warning "$link 是一个普通文件夹（不是链接），为了不误删你的文件，跳过。确认没用后手动删掉再运行。"
        continue
    }
    if ($isLink) {
        # 只删链接本身，不会删链接指向的文件
        [IO.Directory]::Delete($link)
    }
    if ($Uninstall) {
        if ($isLink) { Write-Host "已删除 $link" }
        continue
    }
    New-Item -ItemType Directory -Force -Path $d | Out-Null
    New-Item -ItemType Junction -Path $link -Target $src | Out-Null
    Write-Host "已链接 $link -> $src"
}

# 桌面快捷方式：双击打开 VideoCut 工作台（审阅页的入口，不需要开着 agent）
$lnk = Join-Path ([Environment]::GetFolderPath('Desktop')) 'VideoCut 工作台.lnk'
if ($Uninstall) {
    if (Test-Path -LiteralPath $lnk) { Remove-Item -LiteralPath $lnk; Write-Host "已删除 $lnk" }
} elseif (-not $NoShortcut) {
    $uvw = Get-Command uvw -ErrorAction SilentlyContinue   # uvw：不弹黑窗口的 uv
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uvw -or $uv) {
        $sh = New-Object -ComObject WScript.Shell
        $s = $sh.CreateShortcut($lnk)
        $s.TargetPath = if ($uvw) { $uvw.Source } else { $uv.Source }
        $s.Arguments = 'run "' + (Join-Path $src 'scripts\workbench.py') + '"'
        $s.WorkingDirectory = $src
        $s.Description = 'VideoCut 工作台：所有粗剪过的 FocuSee 工程，不开 agent 也能审阅、编辑、保存'
        $s.IconLocation = (Join-Path $src 'assets\icon\videocut.ico') + ',0'
        if (-not $uvw) { $s.WindowStyle = 7 }   # 没有 uvw 时最小化启动，窗口一闪就关
        $s.Save()
        Write-Host "已在桌面放好「VideoCut 工作台」快捷方式"
    }
}

if (-not $Uninstall) {
    Write-Host ""
    Write-Host "装好了。重开 agent 的会话后就能用：直接说「帮我粗剪最新的 FocuSee 工程」。"
    foreach ($tool in 'uv', 'ffmpeg', 'ffprobe') {
        if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
            Write-Warning "没找到 $tool，请先安装并加进 PATH（见 README 的「环境要求」）。"
        }
    }
    if (-not [Environment]::GetEnvironmentVariable('DASHSCOPE_API_KEY', 'User') -and -not $env:DASHSCOPE_API_KEY) {
        Write-Warning "还没设置 DASHSCOPE_API_KEY（语音识别要用），设置方法见 README。"
    }
}
