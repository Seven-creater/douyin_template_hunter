"""模板注册表(2026-10-21 终版:用户认证模板)。

保留 10 个已验证效果好的(01-10 号文件夹),新增 10 个来自用户提供的
30 个热门翻拍清单(在 templates_ext.py,编为 11-20 号)。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Template:
    name: str                      # 文件夹名(data/NN_<name>)
    desc: str                      # 模板定义(给 GLM 验收用)
    queries: list[str]             # 搜索词(按优先级)
    title_must: list[str]          # 标题须含任一(粗筛)
    title_reject: list[str] = field(default_factory=list)
    duration: tuple[int, int] = (8, 180)   # 秒
    forms_ok: str = ""             # 合格形式说明
    forms_bad: str = ""            # 不合格情形说明
    # 关键词生态弱的模板(2026-10-21 用户指定:尼克狐你被捕了等)——
    # 搜索词几乎搜不到演绎版,只能从种子出发沿推荐流慢慢刷,靠 GLM 验收
    browse_only: bool = False

    def prefilter_title(self, title: str) -> bool:
        if not any(k in title for k in self.title_must):
            return False
        return not any(k in title for k in self.title_reject)


# 共同的不合格标准(2026-10-21 三次审查后的终版)
COMMON_BAD = (
    "纯歌曲/舞蹈/音频展示类视频(观众看不出它想表达什么,无叙事)、"
    "静态图配音乐、演唱会/演出宣传、"
    "某一部短剧/漫剧的正片剧集或切片(同一部剧的连续内容,不是大众翻拍)、"
    "同一影视/综艺作品的【其他桥段】——本模板指定的是某一个具体剧本,"
    "同作品别的名场面不算(注意:用其他IP的角色来演【本模板这场戏】"
    "属于合格跨界演绎,不是其他桥段)、"
    "梗科普解说、纯文字/录屏、与该梗无关只蹭字眼的内容")


def _narrative(name, desc, queries, must, forms_ok, reject=(), dur=(8, 180)):
    return Template(
        name=name, desc=desc, queries=queries, title_must=must,
        title_reject=list(reject), duration=dur,
        forms_ok=forms_ok, forms_bad=COMMON_BAD)


TEMPLATES: dict[str, Template] = {}


def register(t: Template) -> Template:
    TEMPLATES[t.name] = t
    return t


# ---------- 01-04:用户认证的初始模板 ----------
register(_narrative(
    "不烧心",
    "『不烧心』餐饮市井叙事模板:良心摊主用好料守价→被质疑贵/同行低价"
    "挤兑→坚守表态(『贵是贵了点但吃了不烧心』『规矩不能坏』)→顾客回流/"
    "对方翻车。观众能看懂『守规矩的良心人最终赢』的完整故事",
    ["不烧心", "不烧心 剧情", "吃了不烧心", "规矩不能坏",
     "全网进入不烧心时代", "贵是贵了点", "包子里的真心"],
    ["不烧心", "规矩不能坏", "贵是贵了点", "全网进入不烧心时代"],
    "AI漫剧风、真人仿拍、商家自演、换行业改编(火锅/理发/汽车/游戏角色"
    "版)——只要是那套『冲突-坚守-回流』剧情骨架都算",
    reject=["是什么梗", "梗百科", "解梗", "解读", "教程", "合集", "盘点"],
    dur=(12, 150),
))

register(_narrative(
    "雪山救狐狸",
    "『雪山救狐狸/酱板鸭复仇』叙事模板:雪山救狐→反被寻仇的反转桥段,"
    "邵氏武侠画风。观众看得懂『好心救狐却遭报应式复仇』的反转故事",
    ["雪山救狐狸", "酱板鸭 复仇", "雪山救狐狸 二创", "雪山救狐狸 模仿"],
    ["雪山救狐狸", "酱板鸭"],
    "AI武侠漫剧风、真人仿拍、换行业/换画风二创——保留『救狐狸→遭寻仇』"
    "反转剧情骨架",
    dur=(10, 180),
))

register(_narrative(
    "华强买瓜",
    "『华强买瓜』叙事模板:《征服》买瓜名场面(『你这瓜保熟吗』→摊牌"
    "开打)的再创作。观众看得懂『买瓜质疑→冲突爆发』的完整桥段",
    ["华强买瓜", "华强买瓜 AI", "华强买瓜 清朝版", "华强买瓜 模仿",
     "华强 买瓜"],
    ["华强买瓜", "华强", "保熟"],
    "AI改编动画、换画风/换时代(清朝版/女生版/民谣版)、真人仿拍——保留"
    "买瓜冲突剧情与标志性台词",
))

register(_narrative(
    "少帅下飞机",
    "『少帅下飞机』叙事模板:出自剧《少帅》末集——张学良弯腰下机、随即"
    "终身软禁的命运转折瞬间。二创用『下飞机/走下舷梯』画面演绎『高光"
    "时刻→命运分岔』的戏剧转折,观众一看就懂",
    ["少帅下飞机", "少帅下飞机 二创", "少帅下飞机 模仿",
     "张学良 下飞机", "少帅下飞机 版"],
    ["少帅下飞机", "少帅", "张学良", "下飞机"],
    "原片段的剧情混剪、真人cos模仿、换人物版本(AI或真人)、配『从此人生"
    "转折』剧情的改编——核心是能看懂的命运转折叙事",
    reject=["是什么梗", "解读", "纪录片", "历史讲解"],
    dur=(5, 180),
))


def get(name: str) -> Template | None:
    return TEMPLATES.get(name)


def all_templates() -> list[Template]:
    return list(TEMPLATES.values())


# 11-20 号:用户提供的热门翻拍清单精选(见 templates_ext.py)
from .templates_ext import register_all as _register_all
_register_all()
