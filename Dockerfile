FROM python:3.11-slim

# Устанавливаем рабочую директорию
WORKDIR /app

# Копируем requirements.txt и ставим зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && PIP_ONLY_BINARY=:all: pip install --no-cache-dir -r requirements.txt

# Копируем весь проект
COPY . .

# Чтобы логи шли сразу в консоль
ENV PYTHONUNBUFFERED=1

# Запускаем бота
CMD ["python", "main.py"]
