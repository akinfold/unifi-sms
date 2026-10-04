FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home sms && mkdir /data && chown sms:sms /data
COPY --chown=sms:sms . .
RUN DATA_DIR=/tmp/build-data python manage.py collectstatic --noinput && rm -rf /tmp/build-data
USER sms
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz/', timeout=3)" || exit 1
ENTRYPOINT ["python", "docker_entrypoint.py"]
