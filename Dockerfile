FROM python:3.13-alpine
LABEL org.opencontainers.image.title="contact-sync" org.opencontainers.image.version="0.1.0" \
      org.opencontainers.image.source="https://github.com/Snake98bm/contact-sync"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
RUN apk add --no-cache tzdata && adduser -D -u 1000 app && mkdir /data && chown app:app /data
WORKDIR /app
COPY app/ /app/
COPY tests/ /tests/
RUN cd /app && python -c "import icon; icon.build()"
USER app
EXPOSE 80
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s CMD wget -qO- http://127.0.0.1:${PORT:-80}/healthz || exit 1
CMD ["python", "main.py"]
