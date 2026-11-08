FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=UTC PORT=6789 CONFIG_DIR=/config
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends gosu tzdata \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py web.py config.py ./
COPY templates/ ./templates/
COPY static/ ./static/
COPY docker-entrypoint.sh /usr/local/bin/nytarr-entrypoint
RUN chmod +x /usr/local/bin/nytarr-entrypoint
EXPOSE 6789
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
 CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','6789')+'/health',timeout=3)" || exit 1
ENTRYPOINT ["nytarr-entrypoint"]
CMD ["python", "app.py"]
