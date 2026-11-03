FROM python:3.12-slim

ENV PYTHONUTF8=1 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/home/agent
WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY personal_agent/ ./personal_agent/
COPY examples/ ./examples/
COPY playground/README.md ./playground/README.md
RUN mkdir -p /app/.personal /app/playground /home/agent \
    && chown -R 10001:10001 /app/.personal /app/playground /home/agent

USER 10001:10001
STOPSIGNAL SIGTERM
# File-oriented Linux profile: PowerShell is not included in this image.
ENTRYPOINT ["python", "-m", "personal_agent", "--deny-tool", "run_shell"]
CMD ["--help"]
