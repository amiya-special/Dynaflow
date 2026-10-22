FROM python:3.11-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends bash ripgrep \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /opt/tools/requirements.txt
RUN pip install --no-cache-dir pip==25.3 \
    && pip install --no-cache-dir -r /opt/tools/requirements.txt
COPY tool_runtime/execute.py /opt/tools/execute.py
ENV OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /work
USER 1000:1000
CMD ["sleep", "infinity"]
