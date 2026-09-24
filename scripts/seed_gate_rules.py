#!/usr/bin/env python3
"""答复门禁预设规则一次性落库脚本。

往主库 gate_rules 表插入四类预设规则（私人隐私 / 违法信息 / 负面情绪 / 其他）。
幂等：按 (category, name) 去重，已存在同名同分类的规则跳过。
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")

from src.gate.reply_gate import invalidate_gate_cache  # noqa: E402
from src.memory.sqlite_store import SQLiteStore  # noqa: E402

MSG_PRIVACY = "抱歉，这类私人问题我不方便回答，咱们聊点别的吧。"
MSG_ILLEGAL = "这个问题涉及法律或政策红线，我无法作答，请你理解。"
MSG_NEGATIVE = "咱们好好说话～有情绪我可以听，但辱骂的话我就不回应了。"
MSG_OTHER = "此类内容不在我的服务范围内，不便回应。"
MSG_PROMPT = "我的系统设定不可修改，请正常提问。"

PRESET_RULES: list[dict] = [
    # ── 私人隐私 ──
    dict(category="private_privacy", category_label="私人隐私", name="婚恋与家庭",
         match_type="keyword",
         pattern="结婚,婚史,离婚,谈恋爱,谈对象,对象,老婆,老公,妻子,丈夫,爱人,孩子,子女,儿子,女儿,父母,父亲,母亲,家里人,家人,相亲",
         intercept_message=MSG_PRIVACY, priority=10),
    dict(category="private_privacy", category_label="私人隐私", name="年龄与生日",
         match_type="keyword",
         pattern="年龄,多大年纪,你几岁,属相,生肖,星座",
         intercept_message=MSG_PRIVACY, priority=10),
    dict(category="private_privacy", category_label="私人隐私", name="收入与资产",
         match_type="keyword",
         pattern="工资,薪水,月薪,年薪,收入多少,存款,身家,几套房,房贷,车贷",
         intercept_message=MSG_PRIVACY, priority=10),
    dict(category="private_privacy", category_label="私人隐私", name="住址与行踪",
         match_type="keyword",
         pattern="住哪里,家住哪,住址,家庭住址,门牌号,你家在哪",
         intercept_message=MSG_PRIVACY, priority=10),
    # ── 违法信息 ──
    dict(category="illegal_info", category_label="违法信息", name="翻墙与代理工具",
         match_type="keyword",
         pattern="梯子,翻墙,科学上网,vpn,V2Ray,V2RAY,Shadowsocks,OpenVPN,WireGuard,Trojan代理,Clash订阅,机场推荐,节点购买",
         intercept_message=MSG_ILLEGAL, priority=30),
    dict(category="illegal_info", category_label="违法信息", name="涉政敏感话题",
         match_type="keyword",
         pattern="国家主席,总书记,政治局,中南海,领导人评价,批评政府,推翻政府,六四事件,台独,港独,疆独,藏独,法轮功,分裂国家,游行示威",
         intercept_message=MSG_ILLEGAL, priority=30),
    dict(category="illegal_info", category_label="违法信息", name="违禁品与违法活动",
         match_type="keyword",
         pattern="买枪,枪支,弹药,毒品,冰毒,大麻,赌博,博彩,诈骗,洗钱,代开发票,假钞,假币,办假证,偷渡",
         intercept_message=MSG_ILLEGAL, priority=30),
    # ── 负面情绪 ──
    dict(category="negative_emotion", category_label="负面情绪", name="辱骂与脏话",
         match_type="keyword",
         pattern="傻逼,傻B,傻X,傻缺,蠢货,白痴,弱智,智障,废物,蠢猪,王八蛋,混蛋,贱人,婊子,去死,滚蛋,给我滚,他妈的,草泥马,狗东西,有病吧",
         intercept_message=MSG_NEGATIVE, priority=20),
    dict(category="negative_emotion", category_label="负面情绪", name="人身攻击与威胁",
         match_type="keyword",
         pattern="打你,砍你,弄死你,收拾你,威胁你,查你全家,问候你家人",
         intercept_message=MSG_NEGATIVE, priority=20),
    # ── 其他 ──
    dict(category="other", category_label="其他", name="广告与引流",
         match_type="keyword",
         pattern="加微信,加个微信,加我好友,私聊我,转账给我,扫码进群,拉人进群,优惠券代领,兼职刷单,点赞返现",
         intercept_message=MSG_OTHER, priority=10),
    dict(category="other", category_label="其他", name="提示词攻击",
         match_type="keyword",
         pattern="忽略之前的指令,忽略以上,无视规则,越狱,DAN模式,重新定义你的角色,你的system prompt,泄露提示词",
         intercept_message=MSG_PROMPT, priority=15),
]


def main() -> int:
    store = SQLiteStore()  # 默认 data/linkora.db
    added, skipped = [], []
    for r in PRESET_RULES:
        dup = None
        for existing in store.list_gate_rules(category=r["category"], limit=1000):
            if (existing.get("name") or "") == r["name"]:
                dup = existing
                break
        if dup:
            skipped.append(f"#{dup['id']} {r['name']}")
            continue
        rid = store.add_gate_rule(
            category=r["category"],
            category_label=r["category_label"],
            name=r["name"],
            match_type=r["match_type"],
            pattern=r["pattern"],
            intercept_message=r["intercept_message"],
            priority=r["priority"],
            enabled=1,
        )
        added.append(f"#{rid} [{r['category_label']}] {r['name']}")
    print(f"新增 {len(added)} 条:")
    for line in added:
        print("  +", line)
    if skipped:
        print(f"跳过已存在 {len(skipped)} 条:")
        for line in skipped:
            print("  =", line)
    print(f"当前总数: {store.count_gate_rules()}（启用 {store.count_gate_rules(enabled=1)}）")
    invalidate_gate_cache()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
