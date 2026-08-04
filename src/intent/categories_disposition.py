"""处置层意图类别定义。

包含 business 和 social（含 7 个子型）共 9 个类别。
"""
from __future__ import annotations

from src.intent.types import IntentCategory, LAYER_DISPOSITION


DEFAULT_INTENTS: list[IntentCategory] = [
# ===================== 处置层 =====================
    IntentCategory(
        id="business",
        name="业务意图",
        layer=LAYER_DISPOSITION,
        definition=(
            "消息包含可被助手处理的行动意图（提问、请求、指令），或涉及工作/生活领域的"
            "具体内容，需要助手产出响应或执行操作。只要存在明确的信息获取、事务办理或"
            "内容生成需求，即属此类。与 social 互斥：一旦命中业务信号即归此类，不再视为社交。"
        ),
        trigger=(
            "含疑问词（什么/怎么/为什么/多少/哪/谁/何时）；含请求动词"
            "（帮我/麻烦/需要/申请/查询/配置/安装/排查/创建）；或命中领域关键词"
            "（审批/考勤/天气/文档/会议/账号/数据/股价等）；或消息较长且非纯社交套话。"
        ),
        evidence_keywords=[
            "问题", "错误", "故障", "报错", "异常", "失败", "不行",
            "怎么", "如何", "为什么", "什么", "哪里", "谁", "多少", "几", "何时", "什么时候",
            "帮我", "麻烦", "需要", "想要", "请求", "申请",
            "配置", "设置", "安装", "部署", "调试", "排查", "解决", "修复",
            "查询", "搜索", "查看", "确认", "核实", "验证",
            "审批", "流程", "工单", "订单", "任务", "项目",
            "账号", "密码", "权限", "登录", "注册", "开通",
            "链接", "地址", "网址", "入口", "页面", "功能",
            "数据", "报表", "统计", "分析", "报告",
            "时间", "日期", "期限", "截止", "过期",
            "金额", "费用", "价格", "预算", "成本",
            "邮件", "通知", "提醒", "公告", "消息",
            "会议", "日程", "安排", "预约", "计划",
            "文档", "资料", "文件", "附件", "下载", "上传",
            "接口", "API", "服务", "服务器", "系统", "平台",
            "股票", "行情", "上市", "发行", "交易", "IPO", "股价",
            "情况", "总结", "说明", "介绍", "对比", "推荐",
            "考勤", "打卡", "待办", "待审批", "通讯录", "联系人",
            "日历", "天气", "温度", "发送", "创建", "新建",
        ],
    ),
    IntentCategory(
        id="social",
        name="社交意图",
        layer=LAYER_DISPOSITION,
        definition=(
            "消息仅为礼节性社交交换（致谢、确认收到、道别、问候/客套），不含任何需要助手"
            "执行或回答的具体请求、问题或指令。此意图下助手应跳过处理（或仅作极简回应）。"
            "是 business 的补集；其下子型仅作语义细分与日志观测，不改变'跳过'判定。"
        ),
        trigger=(
            "短消息且命中社交词表，且不含 business 证据。与 business 互斥："
            "若同时含业务信号，则优先归 business。"
        ),
        evidence_keywords=[],
    ),
    IntentCategory(
        id="social.gratitude",
        name="致谢",
        layer=LAYER_DISPOSITION,
        parent="social",
        definition="表达谢意、认可对方付出的消息，无后续行动诉求。",
        trigger="含感谢类词（谢谢/感谢/辛苦了/感恩等）且消息较短（≤ pure_thank_max_length）。",
        evidence_keywords=[
            "谢谢", "感谢", "辛苦了", "辛苦", "谢了", "多谢", "太感谢了",
            "感恩", "感激", "谢谢了", "谢谢啊", "谢谢啦", "多谢了",
            "辛苦了啊", "辛苦了啦",
            # —— 扩充（更口语/网络/地域变体）——
            "多谢啦", "谢啦", "thank", "thanks", "thank you", "thx", "3q",
            "蟹蟹", "辛苦您", "受累了", "感谢感谢", "多谢多谢", "感激不尽",
            "不胜感激", "比心", "么么哒", "谢啦啊",
        ],
        max_length=None,
        short_label="thank_you",
    ),
    IntentCategory(
        id="social.acknowledge",
        name="确认收到",
        layer=LAYER_DISPOSITION,
        parent="social",
        definition="确认已读/知晓对方消息，无新请求或问题。",
        trigger=(
            "含确认类词（收到/好的/明白/OK 等，短英文词边界匹配防误命中品牌名）"
            "且消息较短（≤ pure_ack_max_length）。超长则降级为 business。"
        ),
        evidence_keywords=[
            "收到", "好的", "明白", "知道了", "了解", "清楚了",
            "OK", "ok", "Ok", "好", "行", "没问题", "可以",
            "好滴", "好嘞", "好哒", "好的好的", "明白了",
            "收到了", "收到啦", "收到收到",
            # —— 扩充（更口语/语气词/网络变体）——
            "嗯", "嗯嗯", "昂", "哦", "哦哦", "晓得", "晓得了",
            "得嘞", "得勒", "欧了", "O了", "k", "K", "OK了",
            "好哦", "好叭", "好呗", "好呢", "成", "行吧", "中", "中中",
            "get", "GET", "妥", "妥了", "放心", "我看看", "看了", "已看", "已读",
        ],
        max_length=None,
        short_label="acknowledge",
    ),
    IntentCategory(
        id="social.closing",
        name="结束语",
        layer=LAYER_DISPOSITION,
        parent="social",
        definition="表达结束对话、暂时离开的意向。",
        trigger="含道别类词（再见/拜拜/晚安/先这样/收工等）。",
        evidence_keywords=[
            "再见", "拜拜", "晚安", "拜拜了", "下次聊", "下次再说",
            "先这样", "就这样", "结束吧", "收工", "下班了",
            "先忙了", "先忙啦", "先走了", "先撤了",
            # —— 扩充（更口语/网络/地域变体）——
            "回头聊", "回见", "拜啦", "拜拜啦", "掰掰", "bye", "Bye", "BYE",
            "88", "晚安啦", "安", "安啦", "睡了", "撤了", "撤啦",
            "溜了", "溜啦", "走了", "下播", "关机", "下线", "我先撤",
            "下次见", "有空聊", "改天聊", "周末聊", "周一见", "明天聊",
            "回聊", "待会见", "回头说",
        ],
        max_length=None,
        short_label="closing",
    ),
    IntentCategory(
        id="social.polite",
        name="招呼/礼貌",
        layer=LAYER_DISPOSITION,
        parent="social",
        definition="礼貌性问候/招呼，不含具体请求。",
        trigger="含问候类词（你好/早上好/下午好/晚上好/吃了吗等）。",
        evidence_keywords=[
            "你好", "您好", "嗨", "Hi", "hi", "Hello", "hello",
            "在吗", "在不在", "有人吗", "请问", "打扰了", "不好意思",
            "抱歉", "打扰一下", "请问一下", "方便吗",
            # —— 扩充（更口语/时段问候/引起注意）——
            "哈喽", "哈啰", "嗨喽", "早", "早啊", "早呀", "早上好",
            "晚上好", "下午好", "中午好", "上午好", "午安", "晚好",
            "在不", "在嘛", "在么", "有人在不", "您好呀", "你好呀",
            "你好啊", "久仰", "幸会", "冒昧打扰", "打扰您", "不好意思啊",
            "劳驾", "借过", "借光", "在不在呀", "方便不", "有空吗", "在吗在吗",
        ],
        max_length=None,
        short_label="polite",
    ),
    IntentCategory(
        id="social.compliment",
        name="称赞/夸奖",
        layer=LAYER_DISPOSITION,
        parent="social",
        definition="对他人表达赞赏/认可，无后续行动诉求。",
        trigger="含赞美类词（厉害/棒/不错/可以等）。",
        evidence_keywords=[
            "厉害", "太强了", "太棒了", "牛啊", "可以啊", "可以呀",
            "优秀", "靠谱", "给力", "绝了", "牛掰", "可以可以", "真行",
            "棒棒哒", "666", "233", "yyds", "带劲", "可太棒了", "牛哇",
        ],
        max_length=None,
        short_label="compliment",
    ),
    IntentCategory(
        id="social.smalltalk",
        name="闲聊/寒暄",
        layer=LAYER_DISPOSITION,
        parent="social",
        definition="无明确目的的日常闲聊，无业务诉求。",
        trigger="非业务类闲聊（天气闲聊/近况询问等）。",
        evidence_keywords=[
            "在忙吗", "忙不忙", "最近好吗", "最近咋样", "最近怎么样",
            "最近在干嘛", "最近在忙啥", "吃饭了吗", "吃了吗", "睡了吗",
            "周末有空吗", "周末干嘛", "最近累不累", "工作顺利吗",
            "最近忙啥呢", "咋样啊", "还好吗", "过得好吗", "最近可好",
            "在干嘛呢", "今天周五了吧", "最近在忙什么",
        ],
        max_length=None,
        short_label="smalltalk",
    ),
    IntentCategory(
        id="social.emotion",
        name="情绪/感叹",
        layer=LAYER_DISPOSITION,
        parent="social",
        definition="纯情绪表达（感叹/吐槽/抱怨），无具体行动诉求。",
        trigger="含感叹词或情绪表达词，但不含具体请求。",
        evidence_keywords=[
            "哈哈", "哈哈哈", "嘿嘿", "嘻嘻", "呵呵", "笑死", "笑哭",
            "无语", "醉了", "服了", "泪目", "破防", "栓Q", "裂开", "emo",
            "绝绝子", "蚌埠住了", "我裂开了", "笑不活了", "麻了", "笑喷", "笑岔气",
        ],
        max_length=None,
        short_label="emotion",
    ),
]
