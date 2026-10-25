# Auto Video Crafting

<h1 align="center">⚠️ 由于分享需要，部分做了脱敏处理，<br>未经测试，可能存在部分BUG ⚠️</h1>

<h3 align="center">DESENSITIZED FOR SHARING · NOT FULLY TESTED · MAY CONTAIN BUGS</h3>

---

> 通过 AI 自动为带时间戳的字幕文件匹配免费 CC0 视频画面，撑满时长生成最终视频。
>
> 版本：v1_2_0

## 工作流程

```
SRT/ASS/VTT 字幕 + 视频总时长
        │
        ▼
[1] 字幕解析（pysubs2）→ 补齐时间轴到 target_duration，绝不留空隙
        │
        ▼
[2] LLM 分批处理（≤30段/批）→ 翻译为英语 + 抽取 2-3 个视觉搜索关键词
        │   （结果写入缓存，重跑跳过）
        ▼
[3] 双源并行搜索（Pexels + Pixabay 同时请求）→ 合并候选
        │   （元数据写入缓存，同关键词重跑免 API 调用）
        ▼
[4] 阶段 A：6 并发搜索 + 下载（I/O 密集，带三级缓存）
[5] 阶段 B：串行转码（CPU 密集，截取/变速≤2x/循环）→ 1080p 30fps
        │   ↑ 所有产物写入移动硬盘（自动检测，v1_2_0）
        ▼
[6] ffmpeg concat 秒拼 → final.mp4 + manifest.json
```

## 特性

- **零版权风险**：仅使用 Pexels + Pixabay 的 CC0 素材，免署名、可商用
- **绝不黑屏**：时长撑满三策略择优（截取 > 变速 ≤2x > 循环）
- **双源并发**：Pexels + Pixabay 同时搜索，延迟减半
- **三级缓存**：视频下载缓存 + 搜索元数据缓存 + LLM 结果缓存，重跑耗时大幅降低
- **两阶段并发水线**：阶段 A 6 并发搜索+下载（I/O 密集），阶段 B 串行转码（CPU 密集）
- **分批 LLM 调用**：自动拆分为 ≤30 段/批，避免输出截断
- **移动硬盘工作区**（v1_2_0）：自动检测移动硬盘，大容量文件存到 `AutoVideoCrafting_Workspace/`，多电脑协作零配置
- **JSON + MP4 双输出**：MP4 开箱即用，JSON 便于手动替换片段重拼
- **统一规格**：所有 clip 强制 1920x1080 30fps H.264，concat 秒拼

## 移动硬盘工作区（v1_2_0）

为支持多电脑协作，所有大容量文件（视频缓存、输出视频）优先存到移动硬盘。

### 自动检测机制

启动时扫描所有盘符（C: 除外）根目录，查找名为 `AutoVideoCrafting_Workspace` 的目录：

```
扫描顺序：D: → E: → F: → ... → Z:
查找：<盘符>:\AutoVideoCrafting_Workspace\
```

找到则使用移动硬盘路径，未找到则回退本地 `./cache/` 和 `./output/`。

### 路径解析优先级

| 优先级 | 来源 | 示例 |
|---|---|---|
| 1（最高） | 命令行 `--output` 参数 | `python main.py --output D:/tmp/` |
| 2 | `.env` 中 `OUTPUT_DIR` / `CACHE_DIR` | 见 `.env.example` |
| 3 | 移动硬盘自动检测 | `E:\AutoVideoCrafting_Workspace\` |
| 4（最低） | 本地项目目录 | `./cache/` 和 `./output/` |

### 多电脑协作流程

1. 在 A 电脑上完成首次处理 → 缓存写入移动硬盘
2. 拔下移动硬盘，插入 B 电脑
3. 在 B 电脑上运行相同命令 → 自动识别移动硬盘缓存，跳过下载/LLM/搜索
4. 仅需重新执行阶段 B（转码）+ 拼接，耗时大幅降低

### 启动提示

```
[STORAGE] 检测到移动硬盘工作区: E:\AutoVideoCrafting_Workspace
[STORAGE]   cache -> E:\AutoVideoCrafting_Workspace\cache
[STORAGE]   output -> E:\AutoVideoCrafting_Workspace\output
```

或未检测到时：

```
[STORAGE] 未检测到移动硬盘，使用本地存储
[STORAGE]   cache -> c:\...\auto_video_crafting\cache
[STORAGE]   output -> c:\...\auto_video_crafting\output
```

## 安装

### 1. 系统依赖

需要 `ffmpeg` 二进制（包含 `ffprobe`）：

- **Windows**：从 <https://ffmpeg.org/download.html> 下载，解压后将 `bin` 加入 PATH
- **macOS**：`brew install ffmpeg`
- **Linux**：`sudo apt install ffmpeg`

验证：

```bash
ffmpeg -version
ffprobe -version
```

### 2. Python 依赖

```bash
cd auto_video_crafting
pip install -r requirements.txt
```

### 3. 配置 API key

复制 `.env.example` 为 `.env`，填入以下 key：

| Key | 获取地址 | 说明 |
|---|---|---|
| `PEXELS_API_KEY` | <https://www.pexels.com/api/> | 必填至少一个素材源 |
| `PIXABAY_API_KEY` | <https://pixabay.com/api/docs/> | 建议配置作为 fallback |
| `LLM_API_KEY` | <https://dashscope.console.aliyun.com/> | 推荐 DashScope 免费 Qwen2.5-7B |
| `LLM_BASE_URL` | — | 默认 DashScope OpenAI 兼容端点 |
| `LLM_MODEL` | — | 默认 `qwen2.5-7b-instruct` |

> Pexels/Pixabay 至少配置一个；LLM 必须配置。

## 使用

### 基本调用

```bash
python main.py --srt input.srt --duration 60
```

输出：

```
output/
├── clips/                 # 每段处理后的 clip_001.mp4, clip_002.mp4...
├── manifest.json          # 数据契约：每段的源、关键词、处理策略
└── final.mp4              # 最终拼接视频
```

### 完整参数

```bash
python main.py \
  --srt input.srt \                  # 必填：字幕文件路径（srt/ass/vtt）
  --duration 60 \                    # 必填：目标视频总时长（秒）
  --lang auto \                      # 可选：auto/en/de/fr/es/pt/it，默认 auto
  --output output/ \                 # 可选：输出目录，默认 ./output/
  --resolution 1920x1080 \           # 可选：默认 1920x1080
  --fps 30                           # 可选：默认 30
```

### 字幕语言说明

- **英语字幕**：`--lang en` 跳过翻译，直接抽关键词搜索
- **拉丁语字幕**（德/法/西/葡/意）：`--lang auto` 自动检测，LLM 翻译为英语后再搜索
- **不支持中文**：MVP 阶段仅支持拉丁语系

## 时长撑满算法

每段画面的目标时长为 `T` 秒，源视频时长为 `V` 秒：

| 条件 | 策略 | 说明 |
|---|---|---|
| `V >= T` | **截取** | 取前 T 秒 |
| `V < T` 且 `V/T >= 0.5` | **变速慢放** | speed = V/T（≤2倍慢放） |
| `V/T < 0.5` | **循环+截取** | 无限循环到覆盖 T 秒 |

**绝对禁止黑屏**：任何路径下输出时长都精确等于 `target_duration`。

## manifest.json 结构

```json
{
  "input_srt": "input.srt",
  "target_duration": 60,
  "resolution": "1920x1080",
  "fps": 30,
  "segments": [
    {
      "index": 1,
      "start": "00:00:00,000",
      "end": "00:00:05,500",
      "duration": 5.5,
      "original_text": "Guten Morgen, heute sprechen wir über...",
      "en_text": "Good morning, today we'll talk about...",
      "keywords": ["sunrise", "morning coffee"],
      "is_gap": false,
      "source": "pexels",
      "video_url": "https://...",
      "video_id": "12345",
      "clip_path": "output/clips/clip_001.mp4",
      "processing": "cut",
      "processing_params": {"speed": 1.0, "cut_from": 0.0, "cut_to": 5.5}
    }
  ],
  "final_video": "output/final.mp4"
}
```

## 手动二次加工流程

若某段画面不满意：

1. 编辑 `manifest.json` 中对应 segment 的 `video_url`（替换为新的 Pexels/Pixabay URL）
2. 重新下载该段视频到 `cache/`
3. 重新处理为 `output/clips/clip_XXX.mp4`（可单独调用 `lib.clip_processor.ClipProcessor.process()`）
4. 用 ffmpeg 重新拼接：

```bash
ffmpeg -f concat -safe 0 -i <(printf "file '%s'\n" output/clips/clip_*.mp4 | sort) -c copy output/final_v2.mp4
```

## 项目结构

```
auto_video_crafting/                # 项目代码（本地，不入库的大文件已迁移）
├── README.md                      # 本文件
├── requirements.txt
├── .env.example                   # API key 模板
├── .env                           # 实际配置（不入库）
├── .gitignore
├── main.py                        # CLI 入口 + 编排水线（两阶段并发）
├── lib/
│   ├── __init__.py
│   ├── config.py                  # 配置 + 移动硬盘自动检测（v1_2_0）
│   ├── srt_parser.py              # pysubs2 解析 + 时间轴补齐 + 语言检测
│   ├── llm_client.py              # LLM 翻译 + 关键词抽取（分批调用 + 降级）
│   ├── llm_cache.py               # LLM 结果缓存
│   ├── stock_client.py            # Pexels + Pixabay 双源（并行搜索 + 元数据缓存）
│   ├── meta_cache.py              # 搜索元数据缓存
│   ├── concurrency.py             # 通用并发工具
│   ├── downloader.py              # 视频下载 + sha1 缓存（线程安全）
│   ├── clip_processor.py          # 时长撑满算法（核心，串行 CPU 密集）
│   └── assembler.py               # concat 拼接 + manifest
├── srt/                           # 输入字幕文件（小，本地保留）
├── cache/                         # 本地缓存目录（空，仅移动硬盘拔出时回退使用）
└── output/                        # 本地输出目录（空，仅移动硬盘拔出时回退使用）

# 移动硬盘（自动检测，v1_2_0）
E:\AutoVideoCrafting_Workspace\    # 跨机器协作工作区
├── cache/                         # 视频缓存 + 元数据缓存 + LLM 缓存（6+ GB）
│   ├── *.mp4
│   ├── meta_*.json
│   └── llm_cache.json
└── output/                        # 输出产物
    ├── clips/
    ├── manifest.json
    └── final.mp4
```

## 风险与限制

| 项目 | 说明 |
|---|---|
| LLM 输出截断 | 自动分批调用（≤30 段/批），避免单次输出超长 |
| LLM 调用失败 | 自动重试 1 次，仍失败则降级为停用词过滤（精度下降但流程不中断） |
| LLM 重复调用 | 结果缓存到 `cache/llm_cache.json`，重跑直接命中 |
| 素材源限流 | Pexels 200/小时，Pixabay 100/分钟；6 并发安全，自动指数退避重试 |
| 搜索 API 重复调用 | 元数据缓存到 `cache/meta_*.json`，同关键词重跑不消耗配额 |
| 关键词零结果 | fallback 到 `["nature", "landscape", "abstract"]` 通用词 |
| 视频下载失败 | 自动尝试候选列表中下一个；多线程下载 per-URL 锁防竞态 |
| 字幕末尾超过 duration | 截断并警告 |
| 字幕末尾不足 duration | 自动追加 gap 段撑满 |
| 不支持中文 | MVP 仅支持拉丁语系字幕 |
| 缓存损坏 | 静默删除损坏文件，重新请求 API |
| 移动硬盘拔出 | 自动回退本地 cache/ 和 output/（缓存丢失但流程不阻塞） |
| 多电脑协作盘符不同 | `auto_detect_workspace()` 按目录名识别，与盘符无关 |

## 设计原则

- **KISS**：最小依赖（4 个 Python 包 + ffmpeg），不引入 Streamlit/Whisper/CLIP
- **YAGNI**：不预留未来扩展接口，只做当前需要
- **SSOT**：所有配置集中在 `config.py`，禁止其他模块硬编码
- **关注点分离**：11 个子模块各司其职（v1_1_0 新增 3 个缓存/并发模块）
- **尽早失败**：函数入口校验参数，缺失 ffmpeg/API key 立即报错退出
- **绝不黑屏**：时长撑满三策略择优
- **数据契约**：JSON + MP4 双输出，便于手动二次加工
- **并发安全**：所有 I/O 操作线程安全，per-key 锁防竞态
- **存储分层**（v1_2_0）：代码/配置/小文件入项目目录，媒体产物入移动硬盘；按目录名识别跨机器可用

## AI 工作流执行规范（v1_2_0）

后续 AI 执行本项目工作流时，必须遵循以下存储规范：

1. **大容量文件一律存到移动硬盘**：视频缓存、搜索元数据、LLM 缓存、输出视频、clips 等
2. **不要修改本地 cache/ 和 output/ 的内容**：这两个目录仅作为移动硬盘拔出时的回退
3. **不要在代码中硬编码盘符**：通过 `auto_detect_workspace()` 或 `.env` 的 `OUTPUT_DIR` / `CACHE_DIR` 配置
4. **启动时检查存储位置**：观察 `[STORAGE]` 日志确认当前使用的存储路径
5. **多电脑协作时**：先确认移动硬盘已插入并被检测到，再开始工作流

## 依赖版本

| 包 | 协议 | 用途 |
|---|---|---|
| `pysubs2` | MIT | 字幕解析（srt/ass/vtt） |
| `requests` | Apache-2.0 | HTTP 客户端 |
| `openai` | MIT | LLM API（OpenAI 兼容协议） |
| `python-dotenv` | BSD-3-Clause | 读取 .env |
