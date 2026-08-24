FROM python:3.11-slim

# node   -> needed for .promdeobf and .beautify (bot.py calls NODE_BIN="node")
# lua5.1 -> needed for .obf (bot.py calls LUA_BIN="lua" when no bundled lua/lua.exe is found)
# curl/unzip -> only needed to fetch and unpack the lune release, removed after
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        unzip \
        ca-certificates \
        nodejs \
        lua5.1 \
    && ln -sf /usr/bin/lua5.1 /usr/bin/lua \
    && rm -rf /var/lib/apt/lists/*

# Install the Linux "lune" binary. The repo only ships lune.exe (Windows-only),
# which is why `.l` fails on Railway with:
#   [Errno 2] No such file or directory: 'lune'
ARG LUNE_VERSION=0.10.5
RUN curl -fsSL -o /tmp/lune.zip \
        "https://github.com/lune-org/lune/releases/download/v${LUNE_VERSION}/lune-${LUNE_VERSION}-linux-x86_64.zip" \
    && unzip -o /tmp/lune.zip -d /usr/local/bin \
    && chmod +x /usr/local/bin/lune \
    && rm /tmp/lune.zip \
    && apt-get purge -y curl unzip && apt-get autoremove -y

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
