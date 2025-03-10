#!/usr/bin/env bash

_APPDIR="$(dirname "$(realpath "$0")")"
PATH="${_APPDIR}/usr/bin:${PATH}"
LD_LIBRARY_PATH="${_APPDIR}/usr/lib:${LD_LIBRARY_PATH}"
export PATH
export LD_LIBRARY_PATH

if [ "$1" == "cli" ]; then
    shift
    exec keepassxc-cli "$@"
elif [ "$1" == "proxy" ]; then
    shift
    exec keepassxc-proxy "$@"
elif [ -v CHROME_WRAPPER ] || [ -v MOZ_LAUNCHED_CHILD ]; then
    exec keepassxc-proxy "$@"
else
    exec keepassxc "$@"
fi
