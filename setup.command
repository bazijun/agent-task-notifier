#!/bin/zsh
SCRIPT_DIR=${0:A:h}
found_python=0
for candidate in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import tomllib' >/dev/null 2>&1; then
    found_python=1
    "$candidate" "$SCRIPT_DIR/setup.py"
    break
  fi
done
if (( ! found_python )); then
  echo "需要 Python 3.11 或更新版本，请先安装再运行 setup.command。"
fi
echo
read "REPLY?按回车关闭窗口…"
