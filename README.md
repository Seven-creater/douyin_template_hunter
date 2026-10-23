# douyin-template-hunter

**Douyin Template Video Hunter** — 全自动抖音模板视频采集系统:自主发现热梗模板 → 五源候选发现 → GLM 视觉两关验收 → 多样性查重 → 分析报告。

采集"模板视频":一个固定剧本被很多不同人翻拍演绎("人换、骨不变")。规则铁律见
[.claude/skills/douyin-hunt-rules/SKILL.md](.claude/skills/douyin-hunt-rules/SKILL.md)。

> 仅供学习研究。数据目录(视频/元数据)不入库;请遵守抖音用户协议与相关法律,
> 采集内容版权归原平台与创作者所有。

## 架构

```
发现层(可插拔,只负责给 aweme_id)          复用层(一行不改,负责验真)
────────────────────────────────      ──────────────────────────
① 用户分享链接 → scripts/import_       统一输出 ID → hunter.douyin.detail_record
   user_links.py(最快,25s/条)          (详情页,未被限)→ play_url
② 外部搜索引擎 site:douyin.com          → hunter.fetch 下载(CDN,未被限)
   (AI 会话内执行)                      → hunter.verify GLM 两关验收
③ 话题最热页 tag_harvest                     (模板匹配 + 演绎版硬标准)
④ 原声页 music_harvest(一页几十个         → dedup 像素 + semantic_dedup 人脸
   同款,拍同款聚宝盆)                   → hunter.store 入库 01..10.mp4
⑤ 站内搜索(唯一受限环节,兜底)                + metadata.json
```

## 用法

```bash
python -m hunter login [--second]        # 扫码登录(独立 profile 持久)
python -m hunter hunt "模板名"            # 采集一个模板(注册表或新名字)
python -m hunter hunt "X" --queries "原句,A 翻拍"   # 自定义搜索词
python -m hunter --port 19224 --profile b hunt "X" --single  # B 号单线
python -m hunter batch [--only A,B]      # 批量采集注册表
python -m hunter analyze data/NN_模板    # GLM 分析 → analysis.md
python -m hunter status                  # 进度
python -m hunter scout                   # 自主发现新模板候选(热榜+验证)
python -I -u scripts/import_user_links.py data/user_links_NN.txt 19225
                                        # 导入用户分享链接(匿名浏览器)
powershell scripts/stop_hunt.ps1         # 停采集
```

## 目录

- `hunter/` 采集核心(browser CDP / douyin 四路发现 / fetch 下载 /
  verify 两关验收 / store 入库 / dedup 双层查重 / glm 客户端 / templates 注册表)
- `data/NN_模板/` 10 视频 + metadata.json + analysis.md;
  `data/rejected_ids.json` 用户否决黑名单; `data/user_links_*.txt` 链接导入清单
- `scripts/` 在用工具(导入链接 / 停进程 / 杀浏览器 / 样本分析)
- `logs/` 最近一轮运行日志; `_archive/` 历史一次性脚本、旧原型、旧日志

## 硬事实(2026-10 实测)

- 站内搜索接口是唯一确认受限的环节(12-40s 间隔+双号也会耗尽,退避可续)
- 详情页接口 / CDN 下载 / 话题第一页 / 原声页:200+ 次调用未见限制
- python 直抓百度/必应被反爬挡;外部搜索须由 AI 会话内搜索工具执行
- 模板分两族:A 影视名场面型(对手戏公式)/ B 原生句式型(单人演绎合法),
  共同本质 = "人换、骨不变";转折点锁定全片 60-85% 处
