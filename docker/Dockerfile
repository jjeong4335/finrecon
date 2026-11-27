FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# /app/data must exist and belong to `app` so the named volume mounted there is writable.
RUN useradd --create-home app && mkdir -p /app/data && chown -R app /app
USER app
EXPOSE 8501
CMD ["streamlit", "run", "dashboard/Home.py", "--server.port=8501", "--server.address=0.0.0.0"]
