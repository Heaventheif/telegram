# ══════════════════════════════════════════════════════════════
# Dockerfile.base — القالب الأساسي
# الـ Dockerfile النهائي يُولَّد تلقائياً من هذا القالب
# بواسطة plugin_loader عند كل تغيير في plugins/
# ══════════════════════════════════════════════════════════════
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND=noninteractive

# ── حزم apt الأساسية الثابتة ─────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# حزم apt مُضافة تلقائياً من الـ plugins
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        unzip \
        aria2 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# أوامر إضافية من الـ plugins
RUN curl -fsSL https://bun.sh/install | BUN_INSTALL=/usr/local bash

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# (لا توجد حزم pip إضافية من الـ plugins)

COPY . .

EXPOSE 10000

CMD ["python", "main.py"]
