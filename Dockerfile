# Dockerfile cho Messenger Auto-Reply Agent
FROM python:3.11-slim

# Cài đặt các thư viện hệ thống cần thiết cho OpenCV, OCR và X11/GUI dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libgl1-mesa-glx \
    libglib2.0-0 \
    libgomp1 \
    libx11-6 \
    libxext6 \
    libxrender1 \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements và cài đặt
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy toàn bộ mã nguồn vào image
COPY . .

# Chạy Dashboard GUI mặc định
CMD ["python", "gui_app.py"]
