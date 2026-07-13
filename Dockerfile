FROM python:3.12-slim

WORKDIR /app

RUN useradd --create-home appuser
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY run_web.py .

RUN mkdir -p data && chown -R appuser:appuser /app
USER appuser

ENV WEB_HOST=0.0.0.0 WEB_PORT=8000
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

CMD ["python", "run_web.py"]
