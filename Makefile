# Установки по умолчанию
PYTHON = python3
PIP = pip
REQ_FILE = requirements.txt
REQ_DEV_FILE = requirements-dev.txt

# -------------------------------
# Установка зависимостей
install:
	$(PIP) install --upgrade pip
	$(PIP) install -r $(REQ_FILE)
	$(PIP) install -r $(REQ_DEV_FILE)

# -------------------------------
# Проверка форматеров
lint:
	flake8 src/
	isort --check-only src/
	black --check src/

format:
	isort src/
	black src/

# -------------------------------
# Запуск тестов
test:
	pytest -v

# -------------------------------
# Генерация requirements.txt
freeze:
	$(PIP) freeze > $(REQ_FILE)

# -------------------------------
# Запуск pre-commit на все файлы
precommit:
	pre-commit run --all-files
