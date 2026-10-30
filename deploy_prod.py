"""
Безопасный автодеплой Creative UGC CRM на боевой сервер.
Использует прямое SFTP-копирование проверенных локальных файлов,
что гарантирует независимость от git-репозитория на сервере.
"""
import os
import sys
import time
from pathlib import Path
import paramiko
from dotenv import load_dotenv

LOCAL_DIR = Path(__file__).resolve().parent

# Загружаем параметры из локального .env.prod или .env (приоритет: .env.prod)
load_dotenv(LOCAL_DIR / ".env.prod")
load_dotenv(LOCAL_DIR / ".env")

SERVER_HOST = os.getenv("DEPLOY_SERVER_HOST", "")
SERVER_PORT = int(os.getenv("DEPLOY_SERVER_PORT", "22"))
SERVER_USER = os.getenv("DEPLOY_SERVER_USER", "root")
SERVER_PASS = os.getenv("DEPLOY_SERVER_PASS") or os.getenv("SERVER_PASS", "")
SSH_KEY_PATH = Path(os.getenv("DEPLOY_SSH_KEY", str(Path.home() / ".ssh" / "id_rsa")))

REMOTE_DIR = "/opt/tg-crm"
REMOTE_DB = "/opt/tg-crm/crm.db"

# Файлы и папки, которые НЕЛЬЗЯ трогать или копировать (чтобы не задеть прод-базу и локальные среды)
IGNORE_NAMES = {
    ".git",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    "artifacts",
    "scratch",
    "temp_frames",
    "test_sample_frames",
    "crm.db",
    "crm.db.bak",
    "crm.db.pre_clean_backup",
    ".env",
    ".env.prod",
    ".dockerignore",
    "deploy_prod.py",
    "deploy_prod.bat",
    "run_remote.ps1",
}


def connect_ssh():
    global SERVER_HOST
    if not SERVER_HOST:
        SERVER_HOST = input("Введите IP или хост сервера (например 1.2.3.4): ").strip()
    print(f"\n📡 Проверяем соединение с сервером {SERVER_HOST}:{SERVER_PORT}...")
    while True:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            if SSH_KEY_PATH.exists():
                try:
                    client.connect(
                        SERVER_HOST,
                        port=SERVER_PORT,
                        username=SERVER_USER,
                        key_filename=str(SSH_KEY_PATH),
                        timeout=5,
                        banner_timeout=5,
                        auth_timeout=5,
                    )
                    print("✅ Успешно подключено по SSH-ключу!")
                    return client
                except paramiko.AuthenticationException:
                    pass

            if SERVER_PASS:
                client.connect(
                    SERVER_HOST,
                    port=SERVER_PORT,
                    username=SERVER_USER,
                    password=SERVER_PASS,
                    timeout=5,
                    banner_timeout=5,
                    auth_timeout=5,
                )
                print("✅ Успешно подключено по паролю из окружения (.env.prod)!")
                return client

            # Интерактивный запрос, если нет ключа и переменной
            import getpass
            entered_pass = getpass.getpass(f"Введите пароль {SERVER_USER}@{SERVER_HOST}: ")
            client.connect(
                SERVER_HOST,
                port=SERVER_PORT,
                username=SERVER_USER,
                password=entered_pass,
                timeout=5,
                banner_timeout=5,
                auth_timeout=5,
            )
            print("✅ Успешно подключено по введенному паролю!")
            return client

        except Exception:
            print("\n" + "=" * 65)
            print("⚠️  СЕРВЕР ПОКА НЕДОСТУПЕН (таймаут соединения).")
            print("Если включен VPN (AmneziaVPN), сервер блокирует зарубежные запросы.")
            print("Пожалуйста, нажмите «Отключить» в AmneziaVPN.")
            print("=" * 65)
            try:
                input("\n👉 Нажмите Enter сразу после отключения VPN (или Ctrl+C для отмены)... ")
            except (KeyboardInterrupt, EOFError):
                print("\nОтменено пользователем.")
                sys.exit(0)


def run_cmd(client, cmd, desc=""):
    if desc:
        print(f"\n▶ [{desc}]")
        print(f"  $ {cmd}")
    stdin, stdout, stderr = client.exec_command(cmd, get_pty=True)
    for line in iter(stdout.readline, ""):
        print("  " + line, end="")
    exit_code = stdout.channel.recv_exit_status()
    if exit_code != 0:
        err = stderr.read().decode("utf-8", errors="replace")
        if err.strip():
            print(f"  ❌ Ошибка: {err}")
        raise RuntimeError(f"Команда завершилась с ошибкой (код {exit_code})")
    return exit_code


def ensure_remote_dir(sftp, remote_dir):
    parts = remote_dir.replace("\\", "/").strip("/").split("/")
    current = ""
    for part in parts:
        current += "/" + part
        try:
            sftp.stat(current)
        except IOError:
            sftp.mkdir(current)


def sync_files_sftp(client):
    print("\n▶ [2/4. Загружаем свежие файлы на сервер через защищенный SFTP]")
    sftp = client.open_sftp()
    ensure_remote_dir(sftp, REMOTE_DIR)

    uploaded_count = 0
    # Обходим файлы проекта
    for root, dirs, files in os.walk(LOCAL_DIR):
        # Исключаем служебные папки
        dirs[:] = [d for d in dirs if d not in IGNORE_NAMES]

        rel_dir = os.path.relpath(root, LOCAL_DIR)
        remote_target_dir = REMOTE_DIR if rel_dir == "." else f"{REMOTE_DIR}/{rel_dir}".replace("\\", "/")
        ensure_remote_dir(sftp, remote_target_dir)

        for filename in files:
            if filename in IGNORE_NAMES or filename.startswith("crm.db"):
                continue

            local_file = os.path.join(root, filename)
            remote_file = f"{remote_target_dir}/{filename}".replace("\\", "/")

            try:
                sftp.put(local_file, remote_file)
                uploaded_count += 1
                if rel_dir != ".":
                    print(f"  ✓ {rel_dir}/{filename}")
                else:
                    print(f"  ✓ {filename}")
            except Exception as e:
                print(f"  ❌ Ошибка загрузки {filename}: {e}")
                raise

    sftp.close()
    print(f"  Всего передано файлов: {uploaded_count}")


def main():
    print("=" * 65)
    print("🚀 АВТОМАТИЧЕСКИЙ БЕЗОПАСНЫЙ ДЕПЛОЙ НА БОЕВОЙ СЕРВЕР")
    app_domain = os.getenv("BASE_URL") or f"https://{SERVER_HOST}"
    print(f"Сервер: {SERVER_HOST} ({app_domain})")
    print("=" * 65)

    client = connect_ssh()

    try:
        # Шаг 1: Резервная копия базы данных
        run_cmd(
            client,
            f"cp {REMOTE_DB} {REMOTE_DB}.bak_$(date +%Y%m%d_%H%M%S) && ls -lh {REMOTE_DB}*",
            "1/4. Создаем бэкап базы данных crm.db",
        )

        # Шаг 2: Загрузка обновлений (новый интерфейс, автономные шрифты, разметка)
        sync_files_sftp(client)

        # Шаг 3: Пересборка Docker-образа
        run_cmd(
            client,
            f"cd {REMOTE_DIR} && docker compose build --no-cache app",
            "3/4. Пересобираем Docker-образ с новым интерфейсом",
        )

        # Шаг 4: Бесшовный перезапуск контейнера
        run_cmd(
            client,
            f"cd {REMOTE_DIR} && docker compose up -d app",
            "4/4. Перезапускаем сервис",
        )

        # Проверка статуса
        run_cmd(
            client,
            f"cd {REMOTE_DIR} && docker compose ps",
            "Проверка запущенных контейнеров",
        )

        print("\n" + "=" * 65)
        print("🎉 БОЕВОЙ СЕРВЕР УСПЕШНО ОБНОВЛЕН!")
        print()
        print("Ссылка на приложение (Telegram Mini App):")
        app_domain = os.getenv("BASE_URL") or f"https://{SERVER_HOST}"
        print(f"🔗 {app_domain}/static/index.html")
        print()
        print("База данных и все задачи в полной безопасности.")
        print("Теперь можно снова включать VPN!")
        print("=" * 65)

    finally:
        client.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ Прервано из-за ошибки: {e}")
        sys.exit(1)
