#!/usr/bin/env bash
# Creates k8s/ manifests for the api-gateway repo.
# Trimmed for a 2-core / 3.6GB VM: gateway=2 replicas, mock=1, no ingress.
# Run from the repo root:   bash setup-k8s.sh
set -e
mkdir -p k8s

cat > k8s/00-namespace.yaml <<'MANIFEST_EOF'
apiVersion: v1
kind: Namespace
metadata:
  name: gateway
MANIFEST_EOF

cat > k8s/10-redis.yaml <<'MANIFEST_EOF'
# ── Redis: PVC + Deployment + Service ────────────────────────────────────────
# NOTE: your compose runs redis with `--save ""` (persistence OFF). That is
# arguably correct for a rate-limiter/cache. Here it is ON (appendonly) so the
# PVC actually does something. See README "The Redis persistence question".
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: redis-data
  namespace: gateway
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 1Gi
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: redis
  namespace: gateway
  labels:
    app: redis
spec:
  replicas: 1
  strategy:
    # RWO volume can only be mounted by one node at a time. A RollingUpdate
    # would try to start the new pod before the old one releases the volume
    # and deadlock. Recreate = kill old, then start new.
    type: Recreate
  selector:
    matchLabels:
      app: redis
  template:
    metadata:
      labels:
        app: redis
    spec:
      containers:
        - name: redis
          image: redis:7-alpine
          args: ["redis-server", "--appendonly", "yes", "--loglevel", "warning"]
          ports:
            - name: redis
              containerPort: 6379
          volumeMounts:
            - name: data
              mountPath: /data
          readinessProbe:
            exec:
              command: ["redis-cli", "ping"]
            initialDelaySeconds: 3
            periodSeconds: 5
            timeoutSeconds: 3
            failureThreshold: 5
          livenessProbe:
            exec:
              command: ["redis-cli", "ping"]
            initialDelaySeconds: 15
            periodSeconds: 10
            timeoutSeconds: 3
          resources:
            requests:
              cpu: 50m
              memory: 64Mi
            limits:
              memory: 256Mi
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: redis-data
---
apiVersion: v1
kind: Service
metadata:
  name: redis
  namespace: gateway
spec:
  type: ClusterIP
  selector:
    app: redis
  ports:
    - name: redis
      port: 6379
      targetPort: redis
MANIFEST_EOF

cat > k8s/20-gateway-config.yaml <<'MANIFEST_EOF'
# ── ConfigMap: your config.yaml verbatim, minus the JWT secret ───────────────
# The secret_key below is a PLACEHOLDER. The real value comes from the Secret
# as the JWT_SECRET_KEY env var. VERIFY your config loader actually prefers the
# env var over the yaml value — if it does not, auth will use the placeholder.
apiVersion: v1
kind: ConfigMap
metadata:
  name: gateway-config
  namespace: gateway
data:
  config.yaml: |
    gateway:
      host: "0.0.0.0"
      port: 8000
      debug: false
      title: "API Gateway"
      version: "1.0.0"
    redis:
      host: "redis"
      port: 6379
      db: 0
      password: null
    rate_limiting:
      enabled: true
      default_limit: 100
      window_seconds: 60
      ban_duration_seconds: 300
    circuit_breaker:
      failure_threshold: 5
      recovery_timeout_seconds: 30
      half_open_max_calls: 3
    retry:
      max_attempts: 3
      backoff_factor: 0.5
      retry_on_status: [500, 502, 503, 504]
    proxy:
      timeout_seconds: 30
      connect_timeout_seconds: 5
    routes:
      - path: "/users"
        target: "http://user-service:8001"
        rate_limit: 100
        strip_prefix: false
        methods: ["GET", "POST", "PUT", "DELETE", "PATCH"]
        auth_required: true
      - path: "/orders"
        target: "http://order-service:8002"
        rate_limit: 50
        strip_prefix: false
        methods: ["GET", "POST", "PUT", "DELETE"]
        auth_required: true
      - path: "/public"
        target: "http://public-service:8003"
        rate_limit: 200
        strip_prefix: true
        methods: ["GET"]
        auth_required: false
      - path: "/mock"
        target: "http://mock-service:8010"
        rate_limit: 1000
        strip_prefix: false
        methods: ["GET", "POST", "PUT", "DELETE", "PATCH"]
        auth_required: false
    jwt:
      secret_key: "PLACEHOLDER-overridden-by-JWT_SECRET_KEY-env"
      algorithm: "HS256"
      expire_minutes: 60
    logging:
      level: "INFO"
      file: "logs/gateway.log"
      max_bytes: 10485760
      backup_count: 5
    caching:
      enabled: true
      default_ttl_seconds: 60
      cacheable_methods: ["GET"]
      cache_prefix: "cache"
---
# A Secret is base64, NOT encryption. Anyone with get-secret RBAC reads it.
# Real clusters: Sealed Secrets, External Secrets Operator, or a cloud KMS.
apiVersion: v1
kind: Secret
metadata:
  name: gateway-secret
  namespace: gateway
type: Opaque
stringData:
  JWT_SECRET_KEY: "change-me-in-production-please"
MANIFEST_EOF

cat > k8s/30-gateway.yaml <<'MANIFEST_EOF'
# ── API Gateway: Deployment (3 replicas) + Service ───────────────────────────
apiVersion: apps/v1
kind: Deployment
metadata:
  name: gateway
  namespace: gateway
  labels:
    app: gateway
spec:
  replicas: 2
  selector:
    matchLabels:
      app: gateway
  template:
    metadata:
      labels:
        app: gateway
    spec:
      containers:
        - name: gateway
          image: gateway:local
          imagePullPolicy: IfNotPresent   # no registry; image is loaded into the node
          ports:
            - name: http
              containerPort: 8000
          env:
            - name: REDIS_HOST
              value: "redis"              # resolves via the Redis Service DNS name
            - name: REDIS_PORT
              value: "6379"
            - name: JWT_SECRET_KEY
              valueFrom:
                secretKeyRef:
                  name: gateway-secret
                  key: JWT_SECRET_KEY
          volumeMounts:
            # subPath mounts a single file instead of clobbering /app.
            # Trade-off: subPath mounts do NOT hot-reload on ConfigMap change.
            # You must `kubectl rollout restart deploy/gateway` after editing it.
            - name: config
              mountPath: /app/config.yaml
              subPath: config.yaml
              readOnly: true
          readinessProbe:
            httpGet:
              path: /admin/health
              port: http
            initialDelaySeconds: 5
            periodSeconds: 5
            timeoutSeconds: 3
            failureThreshold: 3
          livenessProbe:
            httpGet:
              path: /admin/health
              port: http
            initialDelaySeconds: 20
            periodSeconds: 10
            timeoutSeconds: 3
            failureThreshold: 3
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
            limits:
              memory: 512Mi
      volumes:
        - name: config
          configMap:
            name: gateway-config
---
apiVersion: v1
kind: Service
metadata:
  name: gateway
  namespace: gateway
spec:
  type: ClusterIP
  selector:
    app: gateway
  ports:
    - name: http
      port: 8000
      targetPort: http
MANIFEST_EOF

cat > k8s/40-mock-service.yaml <<'MANIFEST_EOF'
# ── Mock downstream ──────────────────────────────────────────────────────────
# Service name + port MUST stay `mock-service:8010` — that string is hardcoded
# as a route target in config.yaml.
apiVersion: apps/v1
kind: Deployment
metadata:
  name: mock-service
  namespace: gateway
  labels:
    app: mock-service
spec:
  replicas: 1
  selector:
    matchLabels:
      app: mock-service
  template:
    metadata:
      labels:
        app: mock-service
    spec:
      containers:
        - name: mock-service
          image: mock-service:local
          imagePullPolicy: IfNotPresent
          ports:
            - name: http
              containerPort: 8010
          readinessProbe:
            httpGet:
              path: /health
              port: http
            initialDelaySeconds: 5
            periodSeconds: 5
          livenessProbe:
            httpGet:
              path: /health
              port: http
            initialDelaySeconds: 20
            periodSeconds: 10
          resources:
            requests:
              cpu: 50m
              memory: 64Mi
            limits:
              memory: 256Mi
---
apiVersion: v1
kind: Service
metadata:
  name: mock-service
  namespace: gateway
spec:
  type: ClusterIP
  selector:
    app: mock-service
  ports:
    - name: http
      port: 8010
      targetPort: http
MANIFEST_EOF

echo "Created:"; ls -1 k8s/
