#!/bin/sh
# zbm-entrypoint — the one entrypoint of every Railway image (docs/deploy/railway.md, "How the images start").
#
# It adapts the platform to the services WITHOUT changing a service's own guards:
#   --port-var NAME        Railway injects PORT; the services read their own variable (FIN_PORT, LEDGER_PORT, ...).
#                          When PORT is set, NAME is set to it. NAME already set to a different value refuses start.
#   --state-dir-var NAME   NAME names the service's data directory (it must be set and absolute). It is created,
#                          owned by the service user and mode 0700 (the services refuse anything else).
#   --state-file-var NAME  NAME names a file (the ledger log); its parent directory is treated as above.
#   --secret-file NAME     NAME is a *_FILE setting (a path the service reads a secret from, owner-only). Railway
#                          holds variables, not files: the value of NAME without its _FILE suffix (e.g.
#                          FIN_STRIPE_SECRET_KEY for FIN_STRIPE_SECRET_KEY_FILE) is written to a 0600 file under
#                          /run/zbm-secrets owned by the service user, NAME is pointed at it, and the value variable
#                          is removed from the service's environment. Setting both refuses start.
#   -- COMMAND...          the service.
#
# Users. The image runs as the service user (uid/gid 10001) by default. A Railway volume is mounted owned by root,
# which that user cannot write, so services with a volume set RAILWAY_RUN_UID=0 (Railway's documented switch): this
# script then starts as root ONLY to create the data directory and the secret files and hand them to uid 10001, and
# runs the service as 10001 through setpriv (no supplementary groups, no_new_privs). The service never runs as root:
# started as root without setpriv, this script refuses.
#
# Signals. This script stays PID 1 and forwards SIGTERM/SIGINT to the service. As PID 1 the service would ignore a
# signal whose disposition is the default (the kernel does not deliver it to PID 1): ledger-rust re-raises SIGTERM
# with the default disposition to die of it, which PID 1 ignores, and it would be SIGKILLed mid-write at the end of
# Railway's draining window instead of stopping cleanly.
set -eu

APP_UID=10001
APP_GID=10001
SECRET_DIR=/run/zbm-secrets

die() {
    echo "zbm-entrypoint: REFUSING TO START — $*" >&2
    exit 1
}

valid_name() {
    case "$1" in
        '' | [!A-Z]* | *[!A-Z0-9_]*) die "invalid variable name: $1" ;;
    esac
}

# value_of NAME: the value of the variable NAME ("" when unset). NAME is checked first; eval sees only [A-Z0-9_].
value_of() {
    valid_name "$1"
    eval "printf '%s' \"\${$1-}\""
}

is_set() {
    valid_name "$1"
    eval "[ -n \"\${$1+x}\" ]"
}

if [ "$(id -u)" = 0 ]; then AS_ROOT=1; else AS_ROOT=0; fi

prepare_dir() {
    # $1: setting name (for messages), $2: directory
    case "$2" in
        /*) ;;
        *) die "$1 must be an absolute path (got '$2')" ;;
    esac
    case "$2" in
        */../* | */.. | */./* | */.) die "$1 must not contain . or .. components (got '$2')" ;;
    esac
    if [ "$AS_ROOT" = 1 ]; then
        [ -L "$2" ] && die "$1: $2 is a symlink"
        mkdir -p "$2"
        chown "$APP_UID:$APP_GID" "$2"
        chmod 0700 "$2"
    else
        mkdir -p "$2" 2>/dev/null || true
        if [ ! -d "$2" ] || [ ! -w "$2" ]; then
            die "$1: $2 is not a directory this user (uid $(id -u)) can write. A Railway volume is owned by root: set RAILWAY_RUN_UID=0 on this service so this script can hand the directory to uid $APP_UID"
        fi
        chmod 0700 "$2" 2>/dev/null || die "$1: cannot chmod 0700 $2 (it must be owned by uid $(id -u))"
    fi
}

write_secret() {
    # $1: the *_FILE setting; its value variable is the same name without _FILE
    file_var=$1
    case "$file_var" in
        *_FILE) value_var=${file_var%_FILE} ;;
        *) die "--secret-file $file_var: the setting must end in _FILE" ;;
    esac
    valid_name "$value_var"
    if ! is_set "$value_var"; then
        return 0
    fi
    value=$(value_of "$value_var")
    unset "$value_var"
    if [ -z "$value" ]; then
        return 0
    fi
    if is_set "$file_var" && [ -n "$(value_of "$file_var")" ]; then
        die "both $value_var and $file_var are set; set only $value_var (Railway) or only $file_var (a file you provide)"
    fi
    target="$SECRET_DIR/$(printf '%s' "$file_var" | tr 'A-Z_' 'a-z-')"
    old_umask=$(umask)
    umask 077
    rm -f "$target"
    printf '%s' "$value" > "$target"
    umask "$old_umask"
    chmod 0600 "$target"
    if [ "$AS_ROOT" = 1 ]; then
        chown "$APP_UID:$APP_GID" "$target"
    fi
    export "$file_var=$target"
    value=""
}

PORT_VAR=""
STATE_DIRS=""
SECRETS=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --port-var) [ "$#" -ge 2 ] || die "--port-var needs a name"; valid_name "$2"; PORT_VAR=$2; shift 2 ;;
        --state-dir-var) [ "$#" -ge 2 ] || die "--state-dir-var needs a name"; valid_name "$2"
            STATE_DIRS="$STATE_DIRS dir:$2"; shift 2 ;;
        --state-file-var) [ "$#" -ge 2 ] || die "--state-file-var needs a name"; valid_name "$2"
            STATE_DIRS="$STATE_DIRS file:$2"; shift 2 ;;
        --secret-file) [ "$#" -ge 2 ] || die "--secret-file needs a name"; valid_name "$2"
            SECRETS="$SECRETS $2"; shift 2 ;;
        --) shift; break ;;
        *) die "unknown option $1 (expected --port-var, --state-dir-var, --state-file-var, --secret-file, --)" ;;
    esac
done
[ "$#" -gt 0 ] || die "no command after --"

if [ -n "$PORT_VAR" ] && [ -n "${PORT-}" ]; then
    case "$PORT" in
        '' | *[!0-9]*) die "PORT must be a number (got '$PORT')" ;;
    esac
    current=$(value_of "$PORT_VAR")
    if [ -n "$current" ] && [ "$current" != "$PORT" ]; then
        die "$PORT_VAR=$current but PORT=$PORT; unset $PORT_VAR (the service listens on PORT) or make them equal"
    fi
    export "$PORT_VAR=$PORT"
fi

for spec in $STATE_DIRS; do
    kind=${spec%%:*}
    name=${spec#*:}
    path=$(value_of "$name")
    [ -n "$path" ] || die "$name is not set: the image keeps its state there (point it at the volume)"
    if [ "$kind" = file ]; then
        case "$path" in
            */*) prepare_dir "$name" "${path%/*}" ;;
            *) die "$name must be an absolute path (got '$path')" ;;
        esac
    else
        prepare_dir "$name" "$path"
    fi
done

if [ -n "$SECRETS" ]; then
    if [ "$AS_ROOT" = 1 ]; then
        mkdir -p "$SECRET_DIR"
        chown "$APP_UID:$APP_GID" "$SECRET_DIR"
    fi
    chmod 0700 "$SECRET_DIR" 2>/dev/null || true
    [ -w "$SECRET_DIR" ] || die "$SECRET_DIR is not writable by uid $(id -u)"
    for name in $SECRETS; do
        write_secret "$name"
    done
fi

if [ "$AS_ROOT" = 1 ]; then
    command -v setpriv >/dev/null 2>&1 || die "started as root and setpriv is missing: the service never runs as root"
    set -- setpriv "--reuid=$APP_UID" "--regid=$APP_GID" --clear-groups --no-new-privs -- "$@"
fi

child=""
# shellcheck disable=SC2317  # invoked by the traps below
forward() {
    if [ -n "$child" ]; then
        kill "-$1" "$child" 2>/dev/null || true
    fi
}
trap 'forward TERM' TERM
trap 'forward INT' INT

"$@" &
child=$!
while :; do
    set +e
    wait "$child"
    status=$?
    set -e
    # wait returns early when a trapped signal arrives; the child is then still alive (or a zombie not yet reaped)
    if kill -0 "$child" 2>/dev/null; then
        continue
    fi
    break
done
exit "$status"
