# Python 3.12 轻量镜像
#
# 不要在这里写完全限定的第三方 registry（例如 docker.1ms.run/...）：
# 带域名的镜像地址会绕过 Docker daemon 的 registry-mirrors 配置。
# 腾讯云上 daemon 已配好 mirror.ccs.tencentyun.com（内网，拉 180MB 基础镜像约 2 秒），
# 而走第三方 registry 实测只有约 100 KB/s 且会卡死在半途。
# 确实需要换源时用 --build-arg BASE_IMAGE=... 覆盖。
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

WORKDIR /app

# PyPI 镜像站点（只含域名）。默认公网阿里云，本地开发也能构建；
# 腾讯云服务器由 compose 的 build args 覆盖为 mirrors.tencentyun.com（内网 4-7 MB/s）。
ARG PIP_MIRROR_HOST=mirrors.aliyun.com

# 安装 uv
RUN pip install --no-cache-dir uv -i "https://${PIP_MIRROR_HOST}/pypi/simple/"

# 先复制依赖清单，利用分层缓存
COPY pyproject.toml uv.lock ./

# uv.lock 里的 sdist/wheels 是写死的 files.pythonhosted.org 绝对地址，
# `uv sync --frozen` 不会理会 UV_INDEX_URL 而直连该地址（国内实测约 15 KB/s，必然卡死）。
# 因此先在这一层把下载前缀改写成镜像站再安装；文件内容一致，uv 仍会按锁文件校验哈希。
# 只改构建上下文里的这份副本，仓库中的 uv.lock 不受影响。
RUN sed -i "s#https://files.pythonhosted.org/packages#https://${PIP_MIRROR_HOST}/pypi/packages#g" uv.lock \
    && UV_INDEX_URL="https://${PIP_MIRROR_HOST}/pypi/simple/" uv sync --frozen --no-dev

# 再复制业务代码
COPY . .

# 直接用镜像内已装好的虚拟环境，避免启动时 `uv run` 重新同步依赖（会尝试下载 dev 依赖）
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8001

CMD ["uvicorn", "ecommerce_service.main:app", "--host", "0.0.0.0", "--port", "8001"]
