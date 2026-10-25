"""lib 包：auto_video_crafting 核心业务逻辑层。

按关注点分离原则拆分为以下子模块：
- config          配置加载
- srt_parser      字幕解析
- llm_client      LLM 翻译 + 关键词抽取
- stock_client    Pexels / Pixabay 素材搜索
- downloader      视频下载 + 缓存
- clip_processor  时长撑满 + 规格统一
- assembler       最终拼接 + manifest 生成
"""

__version__ = "1.0.0"
