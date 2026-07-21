FROM python:3.12-alpine

RUN pip install --no-cache-dir ninecli==0.1.7
RUN addgroup -S app && adduser -S -G app app
RUN mkdir -p /data/ninebot && chown -R app:app /data/ninebot
WORKDIR /app
COPY --chown=app:app server.py /app/server.py
USER app
VOLUME ["/data/ninebot"]
EXPOSE 19009
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD wget -q -O /dev/null http://127.0.0.1:19009/healthz || exit 1
CMD ["python", "/app/server.py"]
