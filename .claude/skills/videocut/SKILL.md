---
name: videocut
description: 粗剪 FocuSee 录屏工程（.focusee）：识别语音后按实测静音剪气口、剪语气词和口误重录段，按语境校对字幕（含专有名词）并按语义断句，给摄像头画面加美颜，直接写回工程的剪辑轨和字幕轨，再打开本地 HTML 审阅页让用户按字幕或时间轴还原、追加剪辑。用户提到 FocuSee 工程、录屏粗剪、剪气口、去语气词、剪口误/重录、字幕校对、摄像头美颜、给 FocuSee 视频自动剪辑时使用。
---

# VideoCut：FocuSee 粗剪

把 FocuSee 工程做到"粗剪完成"的状态：气口、语气词、重录都剪好，字幕校对过、断句顺眼、放在录屏窗口底部，摄像头画面美颜过。用户再回 FocuSee 精修（缩放、标注、导出），不用再去剪映。

## 关键事实（先读）

- `.focusee` 是文件夹；编辑信息全在 `configure.focuseeproj`（JSON）。剪辑 = `playTracks`（0–1 的源时间比例，段之间的空隙就是剪掉的部分）。缩放、标注、字幕都用**源时间**，所以只改 `playTracks` 和 `subtitles`，其他轨道不动。
- 工作文件放在 `%LOCALAPPDATA%\videocut\<工程名>-<hash>\`：`transcript.json`、`edit.json`（剪辑方案）、`sentences.txt`、`subtitles.preview.srt`、`proxy.mp4`、`audio.wav`、`silence.json`、`original.focuseeproj`。
- 用户在 FocuSee 里手动剪掉的部分**永远不会被恢复**；`sentences.txt` 里用 ✗ 标出，不用管这些句子。
- 每次写入都会在工程旁边生成 `configure.focuseeproj.bak-时间戳`。
- 术语表 `%LOCALAPPDATA%\videocut\hotwords.txt`（一行一个标准写法）所有工程共用：识别时作为热词提交，`sentences.txt` 开头也会列出来给你校对时参考。
- 识别用阿里百炼 `fun-asr`，需要环境变量 `DASHSCOPE_API_KEY`，音频会上传到阿里云。**不要替用户输入或打印 key 的值**；没有就让用户自己用 `setx DASHSCOPE_API_KEY "..."` 设置。

## 剪辑原则（两层判断）

1. **程序只按音频证据自动剪**：实测静音超过 0.3s 的气口（剪完留约 0.18s，前多后少）、开头结尾空白、没人声也没操作的空片段、孤立的「嗯/呃」、前面有停顿的「啊/哦」。切点落在波形最安静处；识别时间戳会把词尾拉长到静音里，程序已按波形把每个词收紧到真正发声的范围，所以不会剪掉轻声的句尾字。
2. **有声音的内容由你（agent）判断**：重录、说一半重来、口误、以及程序标成"建议"的「那个/就是/然后」。判断顺序：
   - 前后是不是同一件事（不是碰巧用了相似的词）；
   - 后一遍是否完整覆盖前一遍的信息；
   - 被删部分有没有独有的提醒、条件、数字、警告、结果或操作；
   - 删掉后前后接起来主语、转折、句意是否完整。

   停顿本身不能证明是重复；文字相似、词表、正则都不能代替语义判断。拿不准就 `confidence: low`（只作建议，不启用），留给用户在审阅页决定。
3. **画面操作保护**：语义剪辑如果和录制时的鼠标点击/按键重叠，程序默认不启用并在审阅页提示用户。滚动不拦（边讲边滚动页面很常见），只在备注里提一句。只有你能确定这段操作在保留的版本里又做了一遍时，才加 `screen: redundant`。

下面命令里 `$S` 指本 skill 的 `scripts` 目录，`$P` 指工程文件夹的绝对路径。所有脚本都用 `uv run` 运行。

## 流程

### 1. 确定工程
- 用户给了路径就用它；没给就运行 `uv run $S/fsproj.py`（默认取 `D:\FocuSee\FocuSee Projects` 下最新修改的工程），把工程名、时长、音频来源告诉用户并确认。
- `focusee_running` 非空时提醒用户：**写入前要在 FocuSee 里关掉这个工程**（最好退出 FocuSee）。

### 2. 识别 + 预览视频
```
uv run $S/transcribe.py --project "$P"
uv run $S/make_proxy.py --project "$P"      # 用 run_in_background，不阻塞后续步骤
uv run $S/beauty.py --project "$P"          # 有摄像头录像时运行，用 run_in_background（实测 16 分钟 720p 录像约 2 小时，先跑起来再做别的）
uv run $S/screen_text.py --project "$P"     # 录屏画面 OCR（本地，不上传），用 run_in_background，给专有名词校对当证据
```
`beauty.py` 给摄像头画面加美颜（FocuSee 自己只有滤镜没有人脸美颜）：参数用默认预设（`assets/beauty_preset.json`，磨皮 16、美白 5、匀肤 8、祛斑祛痘 100、祛法令纹 9、小脸 23、窄脸 8），用户另有 `%LOCALAPPDATA%\videocut\beauty.json` 时以它为准。它按原时间戳重新编码 `recording/webcam-0.mp4`，原文件改名为 `webcam-0.original.mp4`，再跑总是从原文件开始（不会叠加），`--restore` 还原，`--preview 60,300` 只出前后对比图（存在工作目录）。替换文件前同样要求 FocuSee 窗口已关。工程没有摄像头录像、或 `camera.isEnable` 是 false 时跳过这步。
同一份音频有缓存，重复运行不会重复计费；`--force` 强制重识别。

### 3. 自动剪辑
```
uv run $S/analyze.py --project "$P" auto
```
输出里的 `audio` 是静音检测参数。参数可用 `--set` 覆盖并记进 `edit.json`：`pause_threshold`（默认 0.3）、`min_pause`（0.18）、`silence_db`（auto；语音被裁就调低到 -35，停顿残留就调高到 -25）。重新运行 `auto` 只替换自动剪辑，保留 agent/用户的剪辑和字幕校对（`--reset` 全部重来）。

### 4. Agent 审稿：重录 + 字幕校对（这一步靠你，必须做）
读完整的 `sentences.txt`（`[id] 起-止 文本`；［］已剪掉，〔词?cN〕是建议，🖱 是句内点击/按键数，✗ 是原工程已剪）。要拿到逐词序号就运行 `show --sentence N`。

把所有决定写进一个 `plan.json`（放在你的临时目录），一次执行：
```json
{
  "decisions": [
    {"sentences": "12-13", "note": "第14句完整重说了同一内容", "kept": "第14句"},
    {"sentence": 20, "words": "0-1", "note": "开头卡壳「它共它共同」", "kept": "它共同构成的前端"},
    {"sentence": 55, "words": "6-10", "confidence": "medium", "note": "没说完就换了说法", "kept": "..."},
    {"start": 301.2, "end": 303.5, "reason": "manual", "note": "..."}
  ],
  "accept": ["c209"],
  "fixes": [
    {"sentence": 16, "text": "你看，点它，它就可以显示你是奇数次还是偶数次，以及点击了多少次。"}
  ],
  "replacements": [["基数次", "奇数次"]],
  "global_replacements": [["javaspring", "JavaScript"]],
  "terms": ["Claude Code", "DeepSeek", "Next.js"]
}
```
```
uv run $S/analyze.py --project "$P" plan --file plan.json
```
- `decisions`：整句用 `sentences`，句内用 `sentence`+`words`（词序号，含两端），也可以直接给秒数。切点会自动落在安静处，并吃掉两侧多余的停顿。每条都写 `note`（为什么剪）和 `kept`（保留的是哪一遍）。
- `accept`：同意程序给出的建议剪辑（〔〕里的 cN）。
- **字幕校对**（`fixes`）：逐句检查识别错误——同音错字（基数→奇数）、人称代词（指网站/程序用「它」）、多出来的字（「放放」「网网页」）、句内的口头禅（「那个」）。给出整句正确的文字，标点照常写（Next.js、GPT-5.5 这类词里的点会保留）；需要强制换行的地方写 `/`，用了 `/` 的句子每段放得下就整段一行，句尾也会断开。只改显示文字，不影响剪辑，程序会自动按字对齐回时间轴。
- **专有名词二次校对（必须做，直接改，不用问用户）**：识别对产品名、工具名、技术词、人名经常写成近音字、拆开的拼音/英文，同一个词前后还会识别成不同写法。按下面的证据顺序逐个定稿，同一个词全片统一，写进 `fixes`/`replacements`，汇总里不用逐条列出：
  1. **画面上写的**（最强）：先看 `screen_terms.txt`（画面上出现过的英文术语和次数）；有疑问的词用 `screen_text.py --find 词1,词2` 查画面上的写法，或 `--at 秒数` 看那一刻屏幕上的文字；还拿不准就用 ffmpeg 截那一刻的 `display-0.mp4` 画面自己看。讲解时屏幕上开着的文章、幻灯片、代码就是标准写法（大小写也照画面）。
  2. **读音要对得上**：改后的词读起来必须像识别出来的音，不能凭空多出音节或换成另一个东西——「code」可以是 Codex，但不会是 Claude Code（多出了 Claude 两个音）；识别成「root」又偶尔成「router」，说明说的是 router 被吞了尾音。识别结果里哪种写法出现得多**不算证据**。
  3. **说话人的习惯**：术语表里的备注（`#` 后面）记着用户常用什么工具、口头怎么叫（如「单说 code 通常指 Codex」）；结合这期视频里用户自己说过的话（「我的项目是用 Codex 推进的」）。
  4. 上面都定不了：只规范大小写和空格，**保留识别出来的那个词**，不要换成另一个产品或概念。
- 把这期出现的专有名词标准写法放进 `terms`，会追加到全局术语表，下次识别直接用作热词；固定的错写→正写映射放进 `global_replacements`（所有工程生效），只对本工程有效的放 `replacements`。
- **用户指出改错时**：除了改这一句，还要把正确写法加进术语表、删掉错的那条，并在术语表里给这个词写一句备注（为什么是它、什么语境下），这样下次不会再犯。

### 5. 检查字幕断句，然后写入
```
uv run $S/apply.py --project "$P" --dry-run    # 生成 subtitles.preview.srt
```
通读 `subtitles.preview.srt`：每行 ≤16 字，按语义断在标点/停顿处，不让「的/了/吗」开头，不把「一个/非常/这三个」和后面的词拆开。哪行别扭就在那句的 `fixes` 里用 `/` 指定换行，再 dry-run。输出里的 `review` 是要复核的项（超过 5 秒的删除、切完以转折词开头、和画面操作重叠没启用的剪辑）。

确认后写入：
```
uv run $S/apply.py --project "$P"
```
退出码 3 表示 FocuSee 的窗口开着（只在托盘里常驻不算）：让用户关掉这个工程的窗口，或确认没打开这个工程后再加 `--force`。

字幕位置由参数 `subtitle_style` 控制：`bottomPadding`/`upPadding` 是字幕离画布底边/顶边的距离，单位是画布高度的 %（实测 bottomPadding=2 时字幕贴着画布最底下）。默认 `"screen"`：按画布比例、背景边距和屏幕尺寸算出录屏窗口下边缘的位置，让字幕落在录屏窗口里、贴着窗口底边（4:3 画布约 10）。用户想自己定可以 `--set 'subtitle_style={"bottomPadding":12,"upPadding":12,"aligment":0}'`（aligment 0 底部、1 居中、2 顶部）。

### 6. 打开审阅页（一定要给用户）
```
uv run $S/review_server.py --project "$P"   # 用 run_in_background，等输出 READY http://127.0.0.1:<port>/
```
它会自动在用户的默认浏览器里打开审阅页；同时把这个地址写进回复里给用户。页面功能：
- 视频「剪后预览」自动跳过被剪部分，并叠加显示最终字幕；「原片」显示完整源视频；
- 时间轴按原因着色，点色块还原/重新剪掉，拖选新增手动剪辑；
- 字幕列表里点字删/留，Shift+点删/留一段，点 ⏸ 切换气口，左侧方块整句删/留；灰色的「未启用的建议」点一下就启用；
- 「逐字稿」视图：全片连成文档读，拖选文字按 Delete 删除（选中的全是已删的就是还原，只还原选中部分），双击一句直接改字幕文字（写 / 强制换行，清空恢复识别原文），单击跳转播放；拖句首的 ⠿ 调换顺序（剪后预览立即按新顺序播放），之后点页头「生成调序工程」；
- Ctrl+Z 撤销，Ctrl+S 或「保存到工程」写回（同样先备份、检查 FocuSee 进程）。

把汇总告诉用户：原时长 → 剪后时长，各类剪了几处，每条重录判断和理由，校对了多少句字幕（专有名词按语境直接改好，不用逐条列），美颜是否已完成（后台还在跑就说一声），备份文件位置，审阅页地址。

### 7. 交还给用户
- 用户也可以口头让你改（"第 30 句别剪""把 2:10 到 2:15 剪掉""这句字幕改成…"），用 `restore` / `cut` / `fix` 改 `edit.json` 后重新 `apply.py`；审阅页刷新即可看到。
- **调换前后顺序要另出新工程**：FocuSee 打开工程时会把 `playTracks` 按时间重新排好（已实测），所以改剪辑轨调不了序；`apply.py`/保存时 moves 不会写进原工程。用户要调序时：
  1. 记录调序：审阅页逐字稿里拖句首 ⠿，或 `analyze.py move --sentences 12-14 --before 3`（`--to-end` 挪到最后，`unmove` 取消）；
  2. 生成：审阅页点「生成调序工程」，或 `uv run $S/reorder.py --project "$P"`（用 run_in_background，最后一行 `DONE {...}`）。它把录屏、摄像头、麦克风、系统声音按新顺序切开重拼（重新编码：NVIDIA → AMD 核显 AMF → libx264 依次尝试；系统 ffmpeg 太新时 NVIDIA 驱动不支持是正常的），鼠标/按键记录、剪辑轨、字幕、缩放轨都换算到新时间，生成 `<原名> 调序.focusee`（新 uniqueID，原工程不动）；被剪掉的部分也一起挪过去，在 FocuSee 里照样能还原；
  3. 新工程的逐字稿、剪辑方案、预览视频也换算好了，要接着审阅就对新工程启动 `review_server.py`；新工程的摄像头已经美颜过，不要再跑 `beauty.py`（会拒绝）。大约每分钟素材要几秒到十几秒，占几 GB 空间。
- 提醒用户：在 FocuSee 里**重新打开**工程才能看到新剪辑和美颜后的摄像头画面；不满意可以把 `.bak-` 文件改回 `configure.focuseeproj` 还原，美颜用 `beauty.py --restore` 还原。
- 结束时停掉后台的 `review_server.py`。

## 子命令速查（analyze.py）

| 命令 | 作用 |
|---|---|
| `auto [--reset] [--set k=v]` | 生成自动剪辑方案 |
| `show [--sentence N]` | 刷新 `sentences.txt`；带 `--sentence` 时打印该句逐词序号和时间（✂ 表示已剪） |
| `plan --file plan.json` | 批量执行 agent 计划（剪辑、接受建议、字幕校对、替换） |
| `retake --sentences 3-5 --note ... --kept ...` | 整句标记为重录 |
| `cut --words 12:3-5 \| --start S --end E [--reason ...] [--note ...]` | 剪句内几个词或一段时间 |
| `accept --ids c3,c7` | 启用建议剪辑 |
| `restore --ids c3,c7` / `restore --sentences 5` | 还原指定剪辑 / 还原某句上除气口外的剪辑 |
| `fix --sentence N --text "..."` / `--clear` | 校对某句字幕 |
| `replace --from A --to B [--global]` | 字幕替换（`--global` 写进所有工程共用的词表） |
| `move --sentences 12-14 --before 3` / `--to-end` | 记录调换顺序（之后用 `reorder.py` 生成调序工程） |
| `unmove [--ids m1]` | 取消调换顺序 |

`plan.json` 里的 `terms` 会追加进全局术语表（下次识别当热词）。

`retake`/`cut` 也接受 `--confidence high|medium|low` 和 `--screen redundant`。每个子命令都会打印最新汇总和需要复核的项。
