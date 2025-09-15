# ---- EagleReach backend (backend/main.py) ----
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

# Install deps
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copy only the backend code
COPY backend ./backend

# Expose & run
EXPOSE 8000
# NOTE: module path is backend.main:app because main.py is inside /backend
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]

