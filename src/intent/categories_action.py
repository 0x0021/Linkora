"""行动层意图类别定义。

包含 query / execute / analyze / communicate / media / monitor / subscribe 共 7 个类别。
"""
from __future__ import annotations

from src.intent.types import IntentCategory, LAYER_ACTION


ACTION_INTENTS: list[IntentCategory] = [
# ===================== 行动层 =====================
    # 行动意图彼此正交、可共存，不做互斥约束；仅保证语义边界清晰。
    IntentCategory(
        id="action.query",
        name="信息查询",
        layer=LAYER_ACTION,
        definition=(
            "获取已有信息、状态或数据，无副作用。包括天气/联网搜索/知识库检索/"
            "文档/通讯录/日历/审批/考勤/未读/消息记录/组织/系统状态等读取类诉求。"
        ),
        trigger="含查询/搜索/了解/疑问词，或指向某类可读信息（天气/审批/考勤/文档/股价等）。",
        evidence_keywords=[
            "查询", "查一下", "查查", "搜索", "搜一下", "搜", "找", "看看", "了解",
            "什么", "怎么", "如何", "为什么", "多少", "哪", "列表", "详情", "状态",
            "记录", "统计", "报表", "情况", "信息", "资料", "文档", "通讯录", "联系人",
            "日历", "日程", "审批", "考勤", "待办", "未读", "历史", "股价", "行情",
            "天气", "温度", "市场", "数据", "帮忙查", "帮我查", "帮我看", "查下",
        ],
    ),
    IntentCategory(
        id="action.execute",
        name="执行操作",
        layer=LAYER_ACTION,
        definition="创建/修改/发送/触发，产生状态变更或副作用的操作类诉求。",
        trigger="含发送/创建/新建/安排/设置/提交/上传/提醒/预约/办理/开通等动作动词。",
        evidence_keywords=[
            "发送", "发消息", "发一下", "创建", "新建", "安排", "设置", "配置",
            "提交", "上传", "提醒", "预定", "预约", "触发", "开启", "开通", "办理",
            "发个", "建个", "写个", "记一下", "定个",
        ],
    ),
    IntentCategory(
        id="action.analyze",
        name="分析生成",
        layer=LAYER_ACTION,
        definition="对已有信息进行加工、归纳、总结、对比或生成新内容。",
        trigger="含分析/总结/生成/报告/建议/对比/评估等动词。",
        evidence_keywords=[
            "分析", "总结", "归纳", "概括", "提炼", "生成", "创作", "撰写", "起草",
            "报告", "建议", "方案", "评估", "评价", "对比", "比较", "差异化",
            "优化", "改进", "提升", "改善", "整理", "汇总", "统计", "计算",
        ],
    ),
    IntentCategory(
        id="action.communicate",
        name="通讯会话",
        layer=LAYER_ACTION,
        definition="与人的消息往来、会话管理（发消息、看未读、查会话信息、搜聊天记录、@某人）。",
        trigger="涉及给人发消息、查看/检索会话与消息记录、@ 提醒等通讯场景。",
        evidence_keywords=[
            "发消息", "发给我", "告诉他", "通知", "群发", "@", "未读",
            "会话", "聊天记录", "消息记录", "转告", "留言",
        ],
    ),
    IntentCategory(
        id="action.media",
        name="媒体处理",
        layer=LAYER_ACTION,
        definition="处理图片/文件/音频/视频等多媒体内容。",
        trigger="含图片/文件/音频/视频/上传/下载等动词。",
        evidence_keywords=[
            "图片", "图像", "照片", "文件", "文档", "附件", "音频", "视频",
            "上传", "下载", "截图", "发图片", "发文件", "传文件", "媒体",
        ],
    ),
    IntentCategory(
        id="action.monitor",
        name="主动盯办",
        layer=LAYER_ACTION,
        definition="持续追踪某事项状态，到期或变更时主动通知。",
        trigger="含监控/跟踪/盯办/关注/预警等动词。",
        evidence_keywords=[
            "监控", "跟踪", "盯", "关注", "提醒", "预警", "监听", "监测",
            "追踪", "跟进", "盯住", "看着点", "留意", "关注一下",
        ],
    ),
    IntentCategory(
        id="action.subscribe",
        name="订阅推送",
        layer=LAYER_ACTION,
        definition="订阅某主题/来源/对象的更新，在其产生新内容时持续推送给用户，属单向信息流订阅。",
        trigger="含订阅/推送/关注动态/有更新告诉我/新消息通知我等持续性信息流订阅诉求。",
        evidence_keywords=[
            "订阅", "推送", "推给我", "关注动态", "动态推送", "有更新", "更新告诉我", "新消息通知",
            "通知我", "实时播报", "播报", "一有", "就发我", "就发给我", "跟进", "持续关注",
            "跟进一下", "实时同步", "同步给我", "别漏了", "盯住", "播报一下",
        ],
    ),
]
