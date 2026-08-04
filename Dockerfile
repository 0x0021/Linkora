FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        tzdata \
        git \
        gosu \
    && rm -rf /var/lib/apt/lists/*

# 安装 dws CLI（钉钉 DWS 命令行工具）与 gosu（非 root 身份切换）
# 方式一：如果有官方安装脚本，使用官方方式
# 方式二：从预编译二进制下载（需要根据实际架构调整）
# 不锁定 dws 版本：安装脚本默认拉取最新稳定版，构建时 dws 版本随官方发布浮动。
# （如确需固定版本，请在构建时传 --build-arg DWS_VERSION=xxx 并自行补充 dws upgrade 步骤。）
RUN ARCH=$(uname -m) && \
    if [ "$ARCH" = "x86_64" ]; then DWS_ARCH="amd64"; \
    elif [ "$ARCH" = "aarch64" ]; then DWS_ARCH="arm64"; \
    else DWS_ARCH="amd64"; fi && \
    echo "检测到架构: $ARCH -> $DWS_ARCH" && \
    curl -fsSL https://dtalkapp.sjtu.edu.cn:443/dwscript/install.sh -o /tmp/install-dws.sh && \
    bash /tmp/install-dws.sh || echo "[警告] dws 安装脚本执行失败或返回非零（若以 volume 挂载 dws 可忽略；否则运行时 entrypoint 会检测并拒绝启动）" && \
    rm -f /tmp/install-dws.sh && \
    if command -v dws >/dev/null 2>&1; then \
        echo "dws 安装成功: $(dws --version 2>&1 || echo 'unknown')"; \
    else \
        echo "警告: dws 未自动安装成功，请手动挂载或在构建阶段提供 dws 二进制"; \
    fi

# 创建非 root 运行用户（app, uid 1000），配合 gosu 在 entrypoint 切换身份，
# 避免以 root 运行 Python/dws 子进程（降低被攻破时的容器权限面）。
RUN useradd -m -u 1000 -s /bin/bash app && \
    mkdir -p /app/data/backups /app/logs && \
    chown -R app:app /app/data /app/logs /home/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x docker/entrypoint.sh && chown -R app:app /app

# 默认以非 root 身份（app, uid 1000）运行；entrypoint 内 gosu 切换为冗余保障，
# 降低容器被攻破时的权限面（此前缺失 USER 指令，gosu 失败时即以 root 运行）。
USER app

EXPOSE 8080

# docker stop 发 SIGTERM 到 PID 1（entrypoint 用 exec 直启 python），
# 由 main.py 的 handle_signal -> shutdown() 优雅退出（取消定时器/join 线程/flush store）
STOPSIGNAL SIGTERM

# dws 凭证实际落地目录（代码读 DWS_CONFIG_DIR / ~/.dws，与 /root/.dingtalk 无关）
VOLUME ["/app/data", "/app/logs", "/home/app/.dws"]

ENTRYPOINT ["docker/entrypoint.sh"]
CMD ["python3", "main.py"]
