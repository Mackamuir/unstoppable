FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    wget unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ARG DEPOT_DOWNLOADER_VERSION=3.4.0
RUN wget -q "https://github.com/SteamRE/DepotDownloader/releases/download/DepotDownloader_${DEPOT_DOWNLOADER_VERSION}/DepotDownloader-linux-x64.zip" \
    -O /tmp/dd.zip \
    && unzip /tmp/dd.zip -d /opt/depotdownloader \
    && chmod +x /opt/depotdownloader/DepotDownloader \
    && rm /tmp/dd.zip

ENV PATH="/opt/depotdownloader:${PATH}"
# Skip ICU requirement for self-contained .NET apps
ENV DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.yaml .
COPY src/ src/
COPY cli /usr/local/bin/cli
RUN chmod +x /usr/local/bin/cli

RUN mkdir -p /app/data /app/output /app/staging

RUN groupadd -r -g 1000 unstoppable \
    && useradd -r -m -u 1000 -g 1000 -d /home/unstoppable -s /bin/false unstoppable \
    && mkdir -p /home/unstoppable/.local \
    && chown -R unstoppable:unstoppable /app /home/unstoppable
USER unstoppable

ENTRYPOINT ["python", "-m", "src.main"]
