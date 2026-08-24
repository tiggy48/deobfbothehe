FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl unzip nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Railway runs Linux, while the archive only includes the Windows lune.exe.
RUN curl -fsSL -o /tmp/lune.zip \
      https://github.com/lune-org/lune/releases/download/v0.10.5/lune-0.10.5-linux-x86_64.zip \
    && unzip -q /tmp/lune.zip -d /usr/local/bin \
    && chmod +x /usr/local/bin/lune \
    && /usr/local/bin/lune --version \
    && rm /tmp/lune.zip

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY promdeobf/package.json ./promdeobf/package.json
RUN cd promdeobf && npm install --omit=dev

COPY . .

CMD ["python", "bot.py"]