#!/usr/bin/env bash
#
# Builds photoshop_plugin/ into a distributable .ccx — a .ccx is just a zip
# archive with the plugin's manifest.json at the archive root (see
# docs/INSTALL.md for the resulting double-click install flow).
#
# Usage:
#   scripts/build_ccx.sh
#
# Output:
#   dist/cpsb-photoshop-<version>.ccx, where <version> is read from
#   photoshop_plugin/manifest.json's "version" field.

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"
plugin_dir="${repo_root}/photoshop_plugin"
manifest="${plugin_dir}/manifest.json"
dist_dir="${repo_root}/dist"

if ! command -v zip >/dev/null 2>&1; then
  echo "error: 'zip' is required to build a .ccx but was not found on PATH" >&2
  exit 1
fi

if [[ ! -f "${manifest}" ]]; then
  echo "error: ${manifest} not found -- is photoshop_plugin/ present?" >&2
  exit 1
fi

stray_node_modules="$(find "${plugin_dir}" -type d -name node_modules -print -quit)"
if [[ -n "${stray_node_modules}" ]]; then
  echo "error: found a node_modules directory under ${plugin_dir}" \
    "(${stray_node_modules}) -- this plugin ships no dependencies;" \
    "remove it before packaging" >&2
  exit 1
fi

version="$(grep -m1 '"version"' "${manifest}" | sed -E 's/.*"version"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/')"
if [[ -z "${version}" ]]; then
  echo "error: could not read a \"version\" field out of ${manifest}" >&2
  exit 1
fi

out_file="${dist_dir}/cpsb-photoshop-${version}.ccx"

mkdir -p "${dist_dir}"
rm -f "${out_file}"

# Zip the plugin directory's *contents*, not the directory itself, so
# manifest.json lands at the archive root as Creative Cloud/Photoshop expect.
(
  cd "${plugin_dir}"
  zip -r -X -q "${out_file}" . -x '*.DS_Store' -x '__MACOSX/*'
)

if [[ ! -f "${out_file}" ]]; then
  echo "error: build finished but ${out_file} was not created" >&2
  exit 1
fi

size_bytes="$(wc -c < "${out_file}" | tr -d '[:space:]')"
echo "Built ${out_file} (${size_bytes} bytes) from photoshop_plugin/ version ${version}"
