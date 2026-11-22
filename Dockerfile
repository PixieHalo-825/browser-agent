# 沙箱镜像：Agent 生成的 Python 代码在此隔离执行
# 构建: docker build -t browser-agent-sandbox .
#
# 安全特性（配合 app/sandbox/docker_sandbox.py 的运行参数）:
#   - 运行时 network_mode=none  → 无网络访问
#   - 运行时 read_only + tmpfs   → 只读根文件系统
#   - 运行时 mem_limit/nano_cpus → 资源上限
#   - 非 root 用户
FROM python:3.11-slim

# Agent 数据处理常用库（联网安装仅发生在构建期，运行期断网）
RUN pip install --no-cache-dir requests pandas numpy

RUN useradd -m -u 1000 sandbox
USER sandbox
WORKDIR /tmp

# 无 CMD：每次执行由 docker run 传入 python -c <code>
