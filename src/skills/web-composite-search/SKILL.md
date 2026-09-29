---
name: web-composite-search
weight: 0.1
fallback_tools:
- web_search
requires_capabilities:
- playwright
version: 2.2.0
description: 智能多引擎聚合搜索工具，专为国内网络优化。集成25+搜索引擎（百度/搜狗/必应/Google/GitHub/ArXiv等），支持智能意图路由、高级搜索语法和多重去重，无需API
  Key，国内直连。
author: weikev05
license: MIT
tags:
- search
- web
- aggregate
- 中文搜索
- 多引擎
- 学术搜索
- 技术搜索
intent_keywords:
- 搜索
- 查一下
- 帮我搜
- 百度
- 新闻
- 最新
- 网上怎么说的
- 百科
- 动态
- 是什么情况
- 是什么公司
- 市值
- 融资
- 找
- 谷歌
- 搜一下
- 查询
- 搜索新闻
- 搜索论文
- 搜索代码
- search
- find
- look up
- 百度搜索
- 谷歌搜索
- 搜
- 查资料
- 查信息
- 搜索信息
- 搜索内容
- 搜搜
- search for
- web search
- 联网搜索
- 搜索一下
intent_categories:
- domain.web_search
system_prompt: '# 技能指令（AI 生成建议）


  ## 匹配领域

  domain.web_search


  ## 触发关键词

  搜索, 查一下, 帮我搜, 百度, 新闻, 最新, 行情, 股价, 汇率, 最近怎么样, 网上怎么说的, 股票, 上市, 公司, 财报, 百科, 动态, 怎么回事,
  是什么情况, 是什么公司, 市值, 融资, 找, 谷歌, 搜一下, 查询, 搜索新闻, 搜索论文, 搜索代码, search, find, look up, 百度搜索,
  谷歌搜索, 搜, 查资料, 查信息, 搜索信息, 搜索内容, 搜搜, search for, web search, 联网搜索, 搜索一下


  ## 说明

  以上内容由 AI 根据 SKILL.md 分析生成。请根据实际需求修改后再确认覆盖。'
---

# web复合搜索

智能多引擎聚合搜索工具，专为国内网络优化。融合中文深度搜索（公众号/学术/财经）与高级搜索能力（Bing/Yandex/site语法），零Google依赖，自动路由至最优引擎组合，返回结构化去重结果。

## 功能特点

- **多引擎聚合**: 集成25+搜索引擎，覆盖通用、学术、财经、技术等领域
- **智能路由**: 根据查询意图自动选择最优引擎组合
- **国内优化**: 所有引擎国内直连，无需代理
- **高级语法**: 支持 site: filetype: intitle: inurl: 等高级搜索语法
- **智能去重**: URL规范化 + 标题相似度 + 内容指纹三重去重
- **自动降级**: 引擎失效时自动切换备用引擎

## 支持的搜索引擎

### 国内通用
- 百度、搜狗、360搜索、必应中文、头条搜索

### 学术搜索
- ArXiv、Wikipedia、Wolfram Alpha

### 财经投资
- 东方财富、集思录、财新

### 技术社区
- Stack Overflow、GitHub

### 国际引擎
- DuckDuckGo、Brave Search、Qwant、Startpage、Yahoo、Mojeek、Ecosia

### 社交媒体
- 搜狗微信（公众号）

## 使用方式

### 基础搜索
```bash
python scripts/search.py "人工智能发展趋势"