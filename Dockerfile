# استخدام نسخة Python مستقرة وخفيفة
FROM python:3.11-slim

# منع Python من توليد ملفات pyc ومن تخزين الـ output في الـ buffer
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# تثبيت التبعيات النظامية (FFmpeg مهم جداً، و libgl1 لـ OpenCV)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libgl1-mesa-glx \
    libglib2.0-0 \
    build-essential \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# تحديد مجلد العمل
WORKDIR /app

# نسخ ملف المتطلبات أولاً للاستفادة من الـ caching
COPY requirements.txt .

# تثبيت PyTorch نسخة CPU فقط لتفادي فشل البناء (Out of Memory) على سيرفرات Render
RUN pip install --no-cache-dir torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu

# تثبيت باقي مكتبات Python
RUN pip install --no-cache-dir -r requirements.txt

# نسخ باقي ملفات المشروع
COPY . .

# إنشاء مجلد التحميلات
RUN mkdir -p downloads models

# تشغيل البوت
CMD ["python", "bot.py"]
