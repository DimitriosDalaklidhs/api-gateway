FROM python:3.12.14-slim

WORKDIR /app

# Debian's security updates first. The base image is rebuilt only every few
# weeks, so between rebuilds its own packages (openssl, zlib, apt...) lag
# behind fixes Debian has already shipped, and the image scan fails on them.
# Then curl, for the Compose healthcheck and for `kubectl exec` in the README
# demos. Its version is not pinned: Debian removes old package versions from
# its mirrors, so a pin breaks the build the day a security update lands.
# hadolint ignore=DL3008
RUN apt-get update && apt-get upgrade -y && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# An unprivileged user to run as. The code stays owned by root, so the process
# can read it but not change it; logs/ is the one directory it may write to.
RUN groupadd --gid 10001 gateway && \
    useradd --uid 10001 --gid 10001 --home-dir /app --no-create-home --shell /usr/sbin/nologin gateway && \
    mkdir -p logs && chown gateway:gateway logs

# Numeric, so Kubernetes can check runAsNonRoot without resolving a name
USER 10001:10001

EXPOSE 8000

# --no-proxy-headers:The rule is never rewrite the client address from X-Forwarded-For.
# uvicorn trusts that header from 127.0.0.1 by default, which is exactly where
# kubectl port-forward traffic comes from. Behind a load balancer,replace it
# with --forwarded-allow-ips=<load balancer address>.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--no-proxy-headers"]
