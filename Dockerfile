FROM python:3.14-slim@sha256:35caba83af2fe6da9a5d922f9b0156f21d1246a48278c58a08bd253303ba42e8

LABEL org.opencontainers.image.source="https://github.com/akinfold/unifi-sms" \
      org.opencontainers.image.description="SMS archive for UniFi LTE Pro" \
      org.opencontainers.image.licenses="MIT"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
WORKDIR /app
COPY requirements-runtime.lock.txt .
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements-runtime.lock.txt && useradd --uid 10001 --create-home sms && mkdir /data && chown sms:sms /data
COPY --chown=sms:sms . .
RUN DATA_DIR=/tmp/build-data python manage.py collectstatic --noinput && rm -rf /tmp/build-data
USER sms
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz/', timeout=3)" || exit 1
ENTRYPOINT ["python", "docker_entrypoint.py"]
