"""hunter — 抖音模板视频自动采集与分析。

流程:CDP 驱动可见 Edge(登录态持久化)→ 搜索模板关键词 →
抓取搜索接口 JSON(标题/作者/BGM/直链)→ 下载视频(直链优先,页面嗅探兜底)
→ data/<NN>_<模板名>/01.mp4..NN.mp4 + metadata.json → GLM 多模态分析。
"""
