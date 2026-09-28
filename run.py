"""
Inicializador do Market Insight AI.
Verifica dependências, cria .env se faltar e sobe o servidor uvicorn.
Uso:  python run.py
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
ENV_FILE = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"


def ensure_env():
    if not ENV_FILE.exists() and ENV_EXAMPLE.exists():
        shutil.copy(ENV_EXAMPLE, ENV_FILE)
        print("→ .env criado a partir de .env.example")


def check_dependencies():
    missing = []
    for mod, pkg in [
        ("fastapi", "fastapi"),
        ("uvicorn", "uvicorn[standard]"),
        ("pandas", "pandas"),
        ("numpy", "numpy"),
        ("requests", "requests"),
        ("dotenv", "python-dotenv"),
        ("iqoptionapi", "git+https://github.com/victalejo/iqoptionapi.git"),
    ]:
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"→ Instalando dependências ausentes: {', '.join(missing)}")
        subprocess.check_call([sys.executable, "-m", "pip", "install", *missing])


def load_env():
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass


def main():
    print("=" * 60)
    print("  Market Insight AI — Iniciando")
    print("=" * 60)

    ensure_env()
    check_dependencies()
    load_env()

    host = os.getenv("HOST", "127.0.0.1")
    port = os.getenv("PORT", "8000")
    has_llm = bool(os.getenv("OPENAI_API_KEY"))

    print(f"→ Servidor: http://{host}:{port}")
    print(f"→ Relatório: {'LLM (OpenAI)' if has_llm else 'Motor quantitativo local (sem OPENAI_API_KEY)'}")
    print("→ Pressione Ctrl+C para encerrar")
    print("=" * 60)

    subprocess.run([
        sys.executable, "-m", "uvicorn",
        "main:app", "--app-dir", str(ROOT / "app"),
        "--host", host, "--port", port,
    ])


if __name__ == "__main__":
    main()