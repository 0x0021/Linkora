> ⚠️ **本文档已归档。** 最新部署说明请以 [`deployment.md`](../deployment.md) 为准。

# Linkora 部署验证指南 (2026-08-14)

## 一、Staging 环境验证清单

### 1.1 认证端点验证

#### 登录接口测试
```bash
# JSON Body 方式登录
curl -X POST http://staging:8080/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "your_password"}' \
  | jq '.'

# 预期响应:
# {
#   "access_token": "eyJ...",
#   "token_type": "bearer",
#   "role": "admin",
#   "expires_in": 86400
# }

# Basic Auth 方式登录 (向后兼容)
AUTH=$(echo -n "admin:your_password" | base64)
curl -X POST http://staging:8080/api/auth/login \
  -H "Authorization: Basic $AUTH" \
  | jq '.'
```

#### 用户信息接口测试
```bash
# 使用 JWT Token 访问
TOKEN="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
curl http://staging:8080/api/auth/me \
  -H "Authorization: Bearer $TOKEN" \
  | jq '.'

# 预期响应:
# {
#   "username": "admin",
#   "role": "admin"
# }
```

### 1.2 安全头验证

```bash
curl -I http://staging:8080/health

# 预期包含以下安全头:
# X-Content-Type-Options: nosniff
# X-Frame-Options: DENY
# Referrer-Policy: no-referrer
# Permissions-Policy: camera=(), microphone=(), geolocation=()
```

### 1.3 速率限制验证

```bash
# 连续错误登录应触发速率限制
for i in {1..10}; do
  curl -s -X POST http://staging:8080/api/auth/login \
    -H "Content-Type: application/json" \
    -d '{"username": "admin", "password": "wrong"}'
  echo ""
done

# 第 5 次后应返回 429 Too Many Requests
```

## 二、前端验证清单

### 2.1 登录流程验证

1. **正常登录流程**
   - [ ] 输入用户名密码，点击登录
   - [ ] 验证成功跳转到主页面
   - [ ] 验证右上角显示用户名和角色
   - [ ] 验证可以退出登录

2. **错误处理**
   - [ ] 输入错误密码，验证显示错误提示
   - [ ] 空表单提交，验证提示填写完整信息
   - [ ] 网络超时，验证友好提示

3. **Token 管理**
   - [ ] 刷新页面后保持登录状态
   - [ ] Token 过期自动跳转到登录页
   - [ ] 多标签页登录状态同步

### 2.2 API 调用验证

```bash
# 验证带认证的 API 调用
TOKEN="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."

# 平台列表
curl http://staging:8080/api/platforms \
  -H "Authorization: Bearer $TOKEN" | jq '.platforms[] | {id, display_name}'

# 系统路径
curl http://staging:8080/api/system/paths \
  -H "Authorization: Bearer $TOKEN" | jq '.'

# 健康检查
curl http://staging:8080/api/platforms/health \
  -H "Authorization: Bearer $TOKEN" | jq '.'
```

## 三、生产环境灰度发布流程

### 3.1 发布前检查清单

- [ ] 所有测试通过 (212 passed, 2 skipped)
- [ ] mypy 类型检查通过
- [ ] 数据库备份完成
- [ ] 配置文件已更新 (auth_enabled, auth_username, auth_password)
- [ ] 日志级别设置为 INFO
- [ ] 监控告警配置完成

### 3.2 灰度发布步骤

```bash
# 1. 备份当前版本
git tag v2.0.0-staging-backup HEAD

# 2. 部署到 staging
git checkout main
./scripts/deploy.sh --env staging --version latest

# 3. 运行冒烟测试
curl -X POST http://staging:8080/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "test"}'

# 4. 监控指标
watch 'curl -s http://staging:8080/api/platforms/health | jq ".overall"'

# 5. 观察 24 小时后，推广到 production
./scripts/deploy.sh --env production --version latest
```

### 3.3 回滚预案

```bash
# 如遇问题，立即回滚
git tag v2.0.0-rollback $(git rev-parse HEAD)
git checkout v2.0.0-staging-backup
./scripts/deploy.sh --env production --tag v2.0.0-staging-backup
```

## 四、监控告警配置

### 4.1 关键指标

| 指标 | 阈值 | 告警方式 |
|------|------|---------|
| 登录失败率 | > 5% | 邮件 + Slack |
| Token 验证失败 | > 1% | 实时告警 |
| API 响应时间 | > 500ms | Slack |
| 内存使用率 | > 80% | 邮件 |
| SQLite 锁定次数 | > 10/min | 实时监控 |

### 4.2 日志聚合

```bash
# 查看认证相关日志
grep -E "auth|login|token" logs/linkora.log | tail -100

# 查看错误日志
grep -E "ERROR|WARN" logs/linkora.log | tail -50
```

## 五、性能基准测试

### 5.1 登录接口压测

```bash
# 并发 100 请求登录接口
ab -n 1000 -c 100 \
  -H "Content-Type: application/json" \
  -p login.json \
  http://staging:8080/api/auth/login

# login.json 内容:
# {"username":"admin","password":"test123"}
```

### 5.2 API 吞吐量测试

```bash
# 获取平台列表 (需要认证)
TOKEN="..."
ab -n 1000 -c 50 \
  -H "Authorization: Bearer $TOKEN" \
  http://staging:8080/api/platforms
```

## 六、安全检查清单

### 6.1 安全扫描

```bash
# OWASP ZAP 扫描
docker run -t zaproxy/zap2docker-stable zap-baseline.py \
  -t http://staging:8080

# SQL 注入检测
sqlmap -u "http://staging:8080/api/auth/login" \
  --data='{"username":"admin","password":"test"}' \
  --batch
```

### 6.2 依赖审计

```bash
# 检查 Python 依赖漏洞
pip-audit --requirement requirements.txt

# 检查 JS 依赖漏洞
npm audit --production
```

---

**最后更新**: 2026-08-14  
**维护者**: Agnes AI Agent
