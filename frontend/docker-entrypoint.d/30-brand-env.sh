#!/bin/sh
# nginx 启动前执行：把品牌环境变量安全写入 brand-env.js。
# 未设置的变量写入空字符串，brand.js 会自动回退到内置默认值。
set -e

mkdir -p /tmp/brand
out=/tmp/brand/brand-env.js

json_escape() {
  awk -v s="$1" 'BEGIN {
    printf "\"";
    for (i = 1; i <= length(s); i++) {
      c = substr(s, i, 1);
      if (c == "\\" || c == "\"") printf "\\";
      printf "%s", c;
    }
    printf "\"";
  }'
}

{
  printf 'window.EDU_QUERY_BRAND_ENV = Object.freeze({\n'
  printf '  BRAND_NAME: %s,\n' "$(json_escape "${BRAND_NAME:-}")"
  printf '  BRAND_SLOGAN: %s,\n' "$(json_escape "${BRAND_SLOGAN:-}")"
  printf '  BRAND_DESCRIPTION: %s,\n' "$(json_escape "${BRAND_DESCRIPTION:-}")"
  printf '  BRAND_AUTHOR: %s,\n' "$(json_escape "${BRAND_AUTHOR:-}")"
  printf '  BRAND_GITHUB: %s\n' "$(json_escape "${BRAND_GITHUB:-}")"
  printf '});\n'
} > "$out"
