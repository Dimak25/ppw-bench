#!/bin/sh
# PPW Bench: read RAPL energy counters without sudo.
#
#   sudo ./install-udev.sh             # install the rule, add you to "powercap"
#   sudo ./install-udev.sh --uninstall # remove the rule
#
# After installing, log out and back in (or run: newgrp powercap).
set -eu

RULE_NAME=99-ppw-bench-powercap.rules
RULE_DST=/etc/udev/rules.d/$RULE_NAME
GROUP=powercap
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ "$(id -u)" -ne 0 ]; then
    echo "Запустите через sudo: sudo $0 $*" >&2
    exit 1
fi

if [ "${1:-}" = "--uninstall" ]; then
    rm -f "$RULE_DST"
    udevadm control --reload-rules 2>/dev/null || true
    echo "Правило удалено. После перезагрузки energy_uj снова будет доступен только root."
    echo "Группу удалить вручную: sudo groupdel $GROUP"
    exit 0
fi

RULE_SRC=$HERE/$RULE_NAME
if [ ! -f "$RULE_SRC" ]; then
    echo "Не найден $RULE_SRC" >&2
    exit 1
fi

if ! getent group "$GROUP" >/dev/null; then
    groupadd --system "$GROUP"
    echo "Создана группа $GROUP"
fi

install -m 0644 "$RULE_SRC" "$RULE_DST"
echo "Установлено правило $RULE_DST"

if command -v udevadm >/dev/null; then
    udevadm control --reload-rules
    udevadm trigger --subsystem-match=powercap --action=add
fi

TARGET_USER=${SUDO_USER:-}
if [ -n "$TARGET_USER" ] && [ "$TARGET_USER" != root ]; then
    usermod -aG "$GROUP" "$TARGET_USER"
    echo "Пользователь $TARGET_USER добавлен в группу $GROUP."
    echo "Перелогиньтесь (или выполните: newgrp $GROUP), чтобы права вступили в силу."
else
    echo "Добавьте пользователя в группу вручную: sudo usermod -aG $GROUP <user>"
fi

echo
echo "Проверка:"
ls -l /sys/class/powercap/intel-rapl:*/energy_uj 2>/dev/null || echo "  зон intel-rapl нет (модуль intel_rapl_common не загружен?)"
