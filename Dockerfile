# 沙箱镜像：Agent 生成的 Python 代码在此隔离执行
# 构建: docker build -t browser-agent-sandbox .
#
# 网络无法直连 Docker Hub / PyPI 时，用构建参数指向镜像源（无需改 Docker 全局配置）：
#   docker build -t browser-agent-sandbox \
#     --build-arg BASE_IMAGE=docker.m.daocloud.io/library/python:3.11-slim \
#     --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple .
#
# 安全特性（配合 app/sandbox/docker_sandbox.py 的运行参数）:
#   - 运行时 network_mode=none  → 无网络访问
#   - 运行时 read_only + tmpfs   → 只读根文件系统
#   - 运行时 mem_limit/nano_cpus → 资源上限
#   - 非 root 用户
ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

# Agent 数据处理常用库（联网安装仅发生在构建期，运行期断网）
# PIP_INDEX_URL 不传时为 unset，pip 走默认源
ARG PIP_INDEX_URL
RUN pip install --no-cache-dir requests pandas numpy

RUN useradd -m -u 1000 sandbox
USER sandbox
WORKDIR /tmp

# 无 CMD：每次执行由 docker run 传入 python -c <code>
