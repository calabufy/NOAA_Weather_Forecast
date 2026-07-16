# build_zip.py — сборка архива исходников для Yandex Cloud Functions.
# Вызывается из scripts/deploy.ps1. Отдельный скрипт, а не Compress-Archive,
# потому что архивы PowerShell YCF распаковывает с нечитаемыми правами
# (директории без записей/атрибутов -> «No module named 'app'» в рантайме).
# Здесь права выставляются явно: 0644 файлам, 0755 директориям.
#
# Все пути — аргументами и абсолютные: скрипт не зависит от текущей директории
# (в PowerShell 5.1 из-за скобок в пути проекта cwd дочерних процессов ненадёжен).
#
# Запуск: python scripts/build_zip.py --root <корень проекта> --out <файл.zip>

from __future__ import annotations

import argparse
import os
import zipfile

INCLUDE_FILES = ("handler.py", "requirements.txt")
INCLUDE_DIR = "app"


def build(root: str, out: str) -> int:
    zf = zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED)

    def add(path: str, arcname: str) -> None:
        info = zipfile.ZipInfo(arcname)
        if arcname.endswith("/"):
            info.external_attr = 0o755 << 16
            zf.writestr(info, "")
        else:
            info.external_attr = 0o644 << 16
            with open(path, "rb") as f:
                zf.writestr(info, f.read())

    for name in INCLUDE_FILES:
        add(os.path.join(root, name), name)
    for dirpath, dirnames, filenames in os.walk(os.path.join(root, INCLUDE_DIR)):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        arcdir = os.path.relpath(dirpath, root).replace(os.sep, "/") + "/"
        add(dirpath, arcdir)
        for fn in sorted(filenames):
            add(os.path.join(dirpath, fn), arcdir + fn)
    count = len(zf.namelist())
    zf.close()
    return count


def main() -> None:
    p = argparse.ArgumentParser(description="Собрать zip исходников для YCF.")
    p.add_argument("--root", required=True, help="корень проекта (абсолютный путь)")
    p.add_argument("--out", required=True, help="путь итогового zip")
    args = p.parse_args()
    count = build(args.root, args.out)
    print(f"zip: {args.out} ({count} записей)")


if __name__ == "__main__":
    main()
