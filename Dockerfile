# استخدام نسخة Python مستقرة وخفيفة
FROM python:3.11-slim

# منع Python من توليد ملفات pyc ومن تخزين الـ output في الـ buffer
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# تثبيت التبعيات النظامية (FFmpeg مهم جداً لعمل البوت)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    build-essential \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# تحديد مجلد العمل
WORKDIR /app

# نسخ ملف المتطلبات أولاً للاستفادة من الـ caching
COPY requirements.txt .

# تثبيت مكتبات Python
RUN pip install --no-cache-dir -r requirements.txt

# نسخ باقي ملفات المشروع
COPY . .

# إنشاء مجلد التحميلات
RUN mkdir -p downloads models

# تشغيل البوت
CMD ["python", "bot.py"]
