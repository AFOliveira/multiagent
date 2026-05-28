#!/bin/sh
set -eu

uid="${GIT_MULTIAGENT_HOST_UID:-1000}"
gid="${GIT_MULTIAGENT_HOST_GID:-1000}"
user="${GIT_MULTIAGENT_CONTAINER_USER:-gitmultiagent}"
home_dir="/home/gitmultiagent"

if getent group "$gid" >/dev/null 2>&1; then
    group_name="$(getent group "$gid" | cut -d: -f1)"
else
    group_name="$user"
    groupadd -g "$gid" "$group_name"
fi

if getent passwd "$uid" >/dev/null 2>&1; then
    user_name="$(getent passwd "$uid" | cut -d: -f1)"
else
    user_name="$user"
    useradd -m -d "$home_dir" -u "$uid" -g "$gid" -s /bin/bash "$user_name"
fi

mkdir -p "$home_dir"
chown "$uid:$gid" "$home_dir"

if [ -n "${GIT_MULTIAGENT_PI_CONFIG_DIR:-}" ] && [ -d "$GIT_MULTIAGENT_PI_CONFIG_DIR/agent" ]; then
    mkdir -p "$home_dir/.pi/agent"
    cp -f "$GIT_MULTIAGENT_PI_CONFIG_DIR/agent/settings.json" "$home_dir/.pi/agent/settings.json"
    cp -f "$GIT_MULTIAGENT_PI_CONFIG_DIR/agent/models.json" "$home_dir/.pi/agent/models.json"
    chown -R "$uid:$gid" "$home_dir/.pi"
    chmod 700 "$home_dir/.pi" "$home_dir/.pi/agent"
    chmod 600 "$home_dir/.pi/agent/settings.json" "$home_dir/.pi/agent/models.json"
fi

if [ -n "${GIT_MULTIAGENT_EXTRA_GROUPS:-}" ]; then
    old_ifs="$IFS"
    IFS=:
    for extra_gid in $GIT_MULTIAGENT_EXTRA_GROUPS; do
        [ -n "$extra_gid" ] || continue
        if getent group "$extra_gid" >/dev/null 2>&1; then
            extra_group="$(getent group "$extra_gid" | cut -d: -f1)"
        else
            extra_group="gitmultiagent-g$extra_gid"
            groupadd -g "$extra_gid" "$extra_group"
        fi
        usermod -aG "$extra_group" "$user_name"
    done
    IFS="$old_ifs"
fi

cat > /etc/sudoers.d/git-multiagent <<EOF
$user_name ALL=(root) NOPASSWD:ALL
Defaults:$user_name env_keep += "HOME PATH GIT_MULTIAGENT_* NPM_CONFIG_* PIP_USER XDG_* GOPATH CARGO_HOME RUSTUP_HOME HTTP_PROXY HTTPS_PROXY NO_PROXY"
EOF
chmod 0440 /etc/sudoers.d/git-multiagent

export HOME="$home_dir"
exec sudo -E -H -u "$user_name" -- "$@"
