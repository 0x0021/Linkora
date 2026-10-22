"""域层意图类别定义。

工具/技能不再各自维护场景关键词，改为声明服务哪些 domain.* 类别；
关键词文本只在此处维护一份。新增工具的场景词 = 在对应类别追加证据词。
"""
from __future__ import annotations

from src.intent.types import IntentCategory, LAYER_DOMAIN


DOMAIN_INTENTS: list[IntentCategory] = [
# ===================== 域层（路由用，单一真源） =====================
    IntentCategory(
        id="domain.weather", name="天气查询", layer=LAYER_DOMAIN,
        definition="天气、温度、气象相关查询。",
        trigger="问天气/气温/下雨/温度/湿度/空气质量等。",
        evidence_keywords=[
            "天气", "气温", "温度", "下雨", "晴天", "阴天", "多云", "下雪",
            "刮风", "台风", "湿度", "紫外线", "空气质量", "天气预报", "几度",
            "冷不冷", "热不热", "带伞", "雨伞", "通勤", "上班", "下班",
            "下周", "周末",
        ],
    ),
    IntentCategory(
        id="domain.web_search", name="联网搜索", layer=LAYER_DOMAIN,
        definition="联网搜索、新闻、百科、公司/股票公开信息检索。",
        trigger="搜/查公开信息、新闻、最新动态。",
        evidence_keywords=[
            "搜索", "查一下", "帮我搜", "百度", "新闻", "最新", "行情", "股价",
            "汇率", "最近怎么样", "网上怎么说的", "股票", "上市", "公司",
            "财报", "百科", "动态", "怎么回事", "是什么情况", "是什么公司",
            "市值", "融资",
        ],
    ),
    IntentCategory(
        id="domain.calendar", name="日程会议", layer=LAYER_DOMAIN,
        definition="日历、会议、预约安排查询，含冲突检测、空闲时段查找与多人共同时间推荐。",
        trigger="问日程/会议/日历/预约，或问有没有冲突/什么时候有空/找大家都有空的时间。",
        evidence_keywords=[
            "日程", "会议", "日历", "今天有什么安排", "本周会议", "我有会议吗",
            "时间安排", "预约", "schedule", "calendar",
            "有冲突吗", "撞车", "日程冲突", "时间冲突", "会不会撞",
            "什么时候有空", "有空时间", "空闲时段", "空闲时间", "空档",
            "找时间", "大家都有空", "共同时间", "合适的开会时间", "什么时候能开",
            "几点开会", "安排会议", "定个会",
        ],
    ),
    IntentCategory(
        id="domain.todo", name="待办任务", layer=LAYER_DOMAIN,
        definition="创建/查看/完成待办与任务提醒。",
        trigger="让助手记任务/提醒/待办，或查待办清单/完成待办。",
        evidence_keywords=[
            "待办", "任务", "todo", "提醒我", "记一下", "帮我创建一个任务",
            "安排一下", "别忘记",
            "有哪些待办", "我的待办", "待办清单", "待办列表", "查待办",
            "看看待办", "待办事项", "未完成待办", "逾期待办", "过期待办",
            "今天到期", "待办完成了没", "把待办", "完成待办", "标记完成",
            "销掉待办", "待办办完了",
        ],
    ),
    IntentCategory(
        id="domain.contact", name="联系人查询", layer=LAYER_DOMAIN,
        definition="查找人、通讯录、谁负责某事项。",
        trigger="找人/查联系人/通讯录/谁负责。",
        evidence_keywords=[
            "找人", "查谁", "联系人", "通讯录", "谁负责", "谁管", "某某在哪",
            "某某的联系方式", "人事",
        ],
    ),
    IntentCategory(
        id="domain.doc", name="文档读取", layer=LAYER_DOMAIN,
        definition="打开/读取钉钉文档内容。",
        trigger="查看/读取文档内容。",
        evidence_keywords=[
            "读取文档", "文档内容", "查看文档", "打开文档", "文档里怎么写的",
            "文档说什么",
        ],
    ),
    IntentCategory(
        id="domain.approval", name="审批", layer=LAYER_DOMAIN,
        definition="OA 审批列表与详情查询。",
        trigger="问我的审批/待审批/审批详情。",
        evidence_keywords=[
            "待审批", "我的审批", "待我审批", "审批待办", "有哪些审批",
            "审批列表", "要我批的", "待办审批", "审批详情", "这个审批",
            "审批内容", "审批单详情", "审批进展", "审批到哪了", "查看审批",
        ],
    ),
    IntentCategory(
        id="domain.attendance", name="考勤", layer=LAYER_DOMAIN,
        definition="打卡、出勤、考勤状态查询。",
        trigger="问我的考勤/打卡/出勤。",
        evidence_keywords=[
            "我的考勤", "打卡记录", "考勤状态", "今天打卡了吗", "出勤情况",
            "考勤打卡", "上班打卡", "我迟到没", "考勤统计",
        ],
    ),
    IntentCategory(
        id="domain.org", name="组织企业", layer=LAYER_DOMAIN,
        definition="当前组织/企业列表查询。",
        trigger="问我的组织/公司/当前企业。",
        evidence_keywords=[
            "我有哪些组织", "我的组织", "加入了哪些公司", "组织列表",
            "公司列表", "有哪些企业", "当前组织", "现在是哪个公司",
            "当前公司", "当前企业", "我在哪个组织", "当前组织是哪个",
        ],
    ),
    IntentCategory(
        id="domain.profile", name="个人资料", layer=LAYER_DOMAIN,
        definition="查询我的个人信息/资料。",
        trigger="问我是谁/我的资料/工号。",
        evidence_keywords=[
            "我是谁", "我的信息", "我的资料", "我的账号", "我是哪个部门的",
            "我的工号", "我的手机号", "我的邮箱", "个人资料", "my profile",
        ],
    ),
    IntentCategory(
        id="domain.unread", name="未读消息", layer=LAYER_DOMAIN,
        definition="未读消息查询。",
        trigger="问未读/新消息/红点。",
        evidence_keywords=[
            "未读", "我没看的消息", "未读消息", "有什么新消息", "漏看的",
            "红点", "unread",
        ],
    ),
    IntentCategory(
        id="domain.conversation_info", name="会话信息", layer=LAYER_DOMAIN,
        definition="群/会话信息查询（成员、群主等）。",
        trigger="问这个群/群成员/群主。",
        evidence_keywords=[
            "群信息", "这个群", "群成员", "谁在群里", "会话详情", "群主是谁",
            "群里有几个人",
        ],
    ),
    IntentCategory(
        id="domain.search_messages", name="聊天记录", layer=LAYER_DOMAIN,
        definition="历史消息/聊天记录检索。",
        trigger="查历史消息/聊天记录。",
        evidence_keywords=[
            "历史消息", "聊天记录", "查记录", "找一下消息", "之前说过",
            "翻聊天", "记录里",
        ],
    ),
    IntentCategory(
        id="domain.ding", name="DING提醒", layer=LAYER_DOMAIN,
        definition="DING 强提醒消息发送。",
        trigger="发DING/强提醒/钉一下。",
        evidence_keywords=[
            "发DING", "DING提醒", "强提醒", "钉一下", "用DING通知",
            "DING消息", "重要提醒", "务必看到",
        ],
    ),
    IntentCategory(
        id="domain.system_status", name="系统状态", layer=LAYER_DOMAIN,
        definition="系统健康检查与监控。",
        trigger="问系统状态/运行是否正常/监控。",
        evidence_keywords=[
            "系统状态", "运行正常吗", "检查一下", "系统怎么样", "健康状态",
            "状态检查", "监控", "system status",
        ],
    ),
    IntentCategory(
        id="domain.message_stats", name="消息统计", layer=LAYER_DOMAIN,
        definition="消息量、活跃用户等数据统计。",
        trigger="问消息统计/数据分析。",
        evidence_keywords=[
            "消息统计", "统计", "多少消息", "处理了多少", "活跃用户",
            "消息趋势", "数据分析", "统计一下",
        ],
    ),
    IntentCategory(
        id="domain.keyword_rules", name="关键词规则", layer=LAYER_DOMAIN,
        definition="自动回复关键词规则的增删查改。",
        trigger="问/改关键词规则。",
        evidence_keywords=[
            "关键词规则", "规则", "添加规则", "禁用规则", "启用规则",
            "查看规则", "规则列表", "自动回复规则",
        ],
    ),
    IntentCategory(
        id="domain.config", name="配置管理", layer=LAYER_DOMAIN,
        definition="查看/修改 bot 运行配置。",
        trigger="问配置/改配置/热更新。",
        evidence_keywords=[
            "配置", "查看配置", "修改配置", "更新配置", "热更新", "改一下",
            "调整一下", "config",
        ],
    ),
    IntentCategory(
        id="domain.media", name="媒体上传", layer=LAYER_DOMAIN,
        definition="上传图片/文件/媒体素材，获取 media_id。",
        trigger="上传图片/文件/截图/二维码。",
        evidence_keywords=[
            "上传图片", "上传文件", "上传媒体", "upload image", "upload media",
            "获取 media_id", "拿到 media_id", "上传二维码", "上传截图",
        ],
    ),
    IntentCategory(
        id="domain.stock", name="股票分析", layer=LAYER_DOMAIN,
        definition="股票/加密货币分析与组合管理。",
        trigger="问股票/股市/财报/投资组合。",
        evidence_keywords=[
            "股票", "股市", "股价", "行情", "美股", "港股", "A股", "加密",
            "crypto", "投资组合", "财报", "分红", "除权", "除息", "涨停",
            "跌停", "龙虎榜", "市值", "融资", "融券", "基本面", "技术面",
            "K线", "趋势", "热门股", "异动", "分析股票",
        ],
    ),
    # ===================== 后补域类别（工具接入时新增） =====================
    IntentCategory(
        id="domain.minutes", name="会议听记", layer=LAYER_DOMAIN,
        definition="AI 听记/会议纪要的列出与内容提取（摘要、待办、转写、基础信息）。",
        trigger="问会议/听记/纪要/会上聊了什么/会议待办，或要求整理会议内容。",
        evidence_keywords=[
            "会议", "听记", "纪要", "会议记录", "会议纪要", "会上聊了", "聊了什么",
            "会议待办", "会议摘要", "整理会议", "会议内容", "上次开会", "开会的结论",
            "会议结论", "录音转写", "会议转写", "会上有啥", "碰头会", "周会", "月会",
        ],
    ),
    IntentCategory(
        id="domain.report", name="日报周报", layer=LAYER_DOMAIN,
        definition="钉钉日志（日报/周报 OA 应用）的只读查询：列出收到/发出的日志、"
                   "读取单份日志正文、搜索可用日志模板。",
        trigger="问日报/周报写了啥/我交的周报/谁给我发过日报/有哪些日志模板，"
                "或要求总结日志进展、提炼日志待办。",
        evidence_keywords=[
            "日报", "周报", "月报", "日志", "钉钉日志", "填日报", "交周报", "写周报",
            "我交的周报", "我的周报", "发我的日报", "收到的日报", "收到谁的日报",
            "周报写了啥", "日报写了啥", "上周周报", "这周周报", "日志模板",
            "周报模板", "日报模板", "周报进展", "日报进展", "总结周报",
            "周报总结", "汇报一下周报", "我发过哪些日志", "填报",
        ],
    ),
    IntentCategory(
        id="domain.aisearch", name="企业语义检索", layer=LAYER_DOMAIN,
        definition="跨类型语义检索企业内部内容（文档/消息/日程/待办/听记/日志/邮件等），"
                   "以及按行为类型追溯（我发给谁/谁发给我/我创建过什么）。",
        trigger="问「跟某主题有关的企业资料/消息/文档有哪些」「我发给谁了什么」"
                "「谁发给我的」「我创建过哪些」，或要求跨来源汇总企业内部信息。",
        evidence_keywords=[
            "搜一下", "全局搜", "全域搜", "跨类型", "企业内搜", "搜企业",
            "都搜", "搜一遍", "统一搜", "相关文档", "相关消息", "相关资料",
            "相关邮件", "相关日程", "相关待办", "相关纪要", "相关日志",
            "有关的", "相关的", "有哪些文档", "有哪些消息", "哪些资料",
            "发给谁", "谁发给我", "谁发给我", "发给过我", "我发过什么",
            "我发给", "我创建过", "我分享过", "我编辑过", "我收到的",
            "行为记录", "行为追溯", "谁给我发", "我给谁发",
        ],
    ),
    IntentCategory(
        id="domain.wiki", name="知识库", layer=LAYER_DOMAIN,
        definition="钉钉知识库（wiki）的枚举与检索：列出知识库空间、在空间/节点内搜索文档与表格。",
        trigger="问知识库/知识空间/团队文档库，或要求查找/列出知识库内容。",
        evidence_keywords=[
            "知识库", "钉钉知识库", "wiki", "知识空间", "团队知识库", "文档库",
            "知识库空间", "知识库节点", "查知识库", "搜知识库", "找知识库",
            "列出知识库", "知识库里", "团队文档", "知识库文档", "知识库搜索",
            "wiki 空间", "知识库内容",
        ],
    ),
    IntentCategory(
        id="domain.oa_approval", name="审批查询", layer=LAYER_DOMAIN,
        definition="钉钉 OA 审批的只读查询：列出/搜索审批表单模板、查询待我审批、查看审批实例详情与已发起记录，以及查询审批表单的字段级配置（含按字段名反查哪些表单含该字段）。",
        trigger="问审批/待审批/我有哪些审批/审批详情/审批表单/发起的审批，或要求查某条审批进度，或盘点某字段（如「项目」）在哪些表单里。",
        evidence_keywords=[
            "审批", "待审批", "我审批", "我的审批", "待我审批", "审批单", "审批流程",
            "审批模板", "审批表单", "审批详情", "审批记录", "已发起审批", "发起的审批",
            "审批进度", "审批任务", "oa 审批", "oa审批", "oa 流程", "要我批的",
            "帮我看下审批", "查审批",
            "表单字段", "字段配置", "表单配置", "哪些表单", "含项目字段", "项目字段",
            "字段有哪些", "表单有哪些字段", "必填字段",
        ],
    ),
]
