#!/usr/bin/env bash
set -euo pipefail

config_dir="${OMNISIGHT_NEO4J_CONFIG_DIR:-$HOME/.config/omnisight/neo4j}"
data_dir="${OMNISIGHT_NEO4J_DATA_DIR:-$HOME/.local/share/omnisight/neo4j}"
cert_days="${OMNISIGHT_NEO4J_CERT_DAYS:-397}"
tls_cn="${OMNISIGHT_NEO4J_TLS_CN:-localhost}"
advertised_address="${OMNISIGHT_NEO4J_ADVERTISED_ADDRESS:-localhost}"
password_file="$config_dir/neo4j-password"
conf_dir="$config_dir/conf"
cert_dir="$config_dir/certificates"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'neo4j preflight: missing required command: %s\n' "$1" >&2
    exit 1
  fi
}

generate_password() {
  if [ -s "$password_file" ]; then
    if grep -Eq '^[A-Za-z0-9._@#%+=,:-]{12,}$' "$password_file"; then
      chmod 600 "$password_file"
      return
    fi
    printf 'neo4j preflight: replacing password with NEO4J_AUTH-safe value\n' >&2
  fi

  if [ -s "$password_file" ]; then
    chmod 600 "$password_file"
  fi

  umask 077
  openssl rand -hex 24 > "$password_file"
  chmod 600 "$password_file"
}

generate_cert() {
  local scope="$1"
  local scope_dir="$cert_dir/$scope"

  mkdir -p "$scope_dir/trusted" "$scope_dir/revoked"

  if [ -s "$scope_dir/private.key" ] && [ -s "$scope_dir/public.crt" ]; then
    chmod 400 "$scope_dir/private.key"
    chmod 444 "$scope_dir/public.crt"
    return
  fi

  umask 077
  openssl req \
    -x509 \
    -newkey rsa:4096 \
    -sha256 \
    -days "$cert_days" \
    -nodes \
    -subj "/CN=$tls_cn" \
    -addext "subjectAltName=DNS:localhost,DNS:host.docker.internal,IP:127.0.0.1" \
    -keyout "$scope_dir/private.key" \
    -out "$scope_dir/public.crt" \
    >/dev/null 2>&1
  chmod 400 "$scope_dir/private.key"
  chmod 444 "$scope_dir/public.crt"
}

write_config() {
  mkdir -p "$conf_dir"
  cat > "$conf_dir/neo4j.conf" <<EOF
server.default_listen_address=0.0.0.0
server.default_advertised_address=$advertised_address

server.bolt.enabled=true
server.bolt.listen_address=:7687
server.bolt.tls_level=OPTIONAL

server.http.enabled=false
server.https.enabled=true
server.https.listen_address=:7473

dbms.ssl.policy.bolt.enabled=true
dbms.ssl.policy.bolt.base_directory=/ssl/bolt
dbms.ssl.policy.bolt.private_key=private.key
dbms.ssl.policy.bolt.public_certificate=public.crt

dbms.ssl.policy.https.enabled=true
dbms.ssl.policy.https.base_directory=/ssl/https
dbms.ssl.policy.https.private_key=private.key
dbms.ssl.policy.https.public_certificate=public.crt

server.memory.heap.initial_size=512m
server.memory.heap.max_size=2G
server.memory.pagecache.size=512m
EOF
  chmod 644 "$conf_dir/neo4j.conf"
}

require_command openssl

mkdir -p "$config_dir" "$data_dir/data" "$data_dir/logs" "$data_dir/import" "$data_dir/plugins"
chmod 700 "$config_dir"
generate_password
generate_cert bolt
generate_cert https
write_config
