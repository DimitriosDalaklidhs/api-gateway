FROM python:3.12.14-slim

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p logs

EXPOSE 8000

# --no-proxy-headers:The rule is never rewrite the client address from X-Forwarded-For.
# uvicorn trusts that header from 127.0.0.1 by default, which is exactly where
# kubectl port-forward traffic comes from. Behind a load balancer,replace it
# with --forwarded-allow-ips=<load balancer address>.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--no-proxy-headers"]
