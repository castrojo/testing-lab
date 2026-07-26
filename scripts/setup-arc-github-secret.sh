#!/usr/bin/env bash
# Setup script for the ARC GitHub App secret.
#
# A contributor with GitHub org admin access and the downloaded private key
# can run this to create the arc-github-secret in the arc-runners namespace.
#
# GitHub does not let you download an existing private key. If the key is lost,
# generate a new one from the app settings page. The new .pem downloads once.
#
# Usage:
#   scripts/setup-arc-github-secret.sh                           # interactive
#   scripts/setup-arc-github-secret.sh /path/to/key.pem          # interactive IDs, explicit PEM path
#   scripts/setup-arc-github-secret.sh --pem-path /path/to/key.pem --app-id 123 --installation-id 456
#
# Environment variables (non-interactive recovery):
#   ARC_APP_ID, ARC_INSTALLATION_ID, ARC_PEM_PATH, ARC_APP_SLUG, ARC_NAMESPACE

set -euo pipefail

NAMESPACE="${ARC_NAMESPACE:-arc-runners}"
SECRET_NAME="arc-github-secret"
APP_SLUG="${ARC_APP_SLUG:-bluefin-ghost-arc}"

APP_ID="${ARC_APP_ID:-}"
INSTALLATION_ID="${ARC_INSTALLATION_ID:-}"
PEM_PATH="${ARC_PEM_PATH:-}"

usage() {
  cat <<EOF
Usage: $0 [OPTIONS] [PEM_PATH]

Options:
  --app-slug APP_SLUG            GitHub App slug (default: ${APP_SLUG})
  --namespace NAMESPACE          Target namespace (default: arc-runners)
  --pem-path PATH                Path to the downloaded .pem private key
  --app-id ID                    GitHub App ID (skips gh API lookup)
  --installation-id ID           GitHub App installation ID (skips gh API lookup)
  -h, --help                     Show this help

Environment variables (all optional):
  ARC_APP_ID, ARC_INSTALLATION_ID, ARC_PEM_PATH, ARC_APP_SLUG, ARC_NAMESPACE
EOF
}

# Parse optional flags. A lone positional argument is treated as PEM_PATH.
while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-slug)
      APP_SLUG="$2"; shift 2 ;;
    --namespace)
      NAMESPACE="$2"; shift 2 ;;
    --pem-path)
      PEM_PATH="$2"; shift 2 ;;
    --app-id)
      APP_ID="$2"; shift 2 ;;
    --installation-id)
      INSTALLATION_ID="$2"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    --)
      shift; break ;;
    -*)
      echo "ERROR: Unknown option: $1" >&2
      usage >&2
      exit 1 ;;
    *)
      if [[ -z "${PEM_PATH}" ]]; then
        PEM_PATH="$1"
      else
        echo "ERROR: Unexpected positional argument: $1" >&2
        usage >&2
        exit 1
      fi
      shift ;;
  esac
done

if ! command -v gh >/dev/null 2>&1; then
  echo "ERROR: gh CLI is required." >&2
  exit 1
fi

if ! command -v kubectl >/dev/null 2>&1; then
  echo "ERROR: kubectl is required." >&2
  exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
  echo "ERROR: gh CLI is not authenticated. Run 'gh auth login' first." >&2
  exit 1
fi

# Resolve App ID and Installation ID from gh unless provided explicitly.
if [[ -z "${APP_ID}" || -z "${INSTALLATION_ID}" ]]; then
  echo "Fetching GitHub App details for ${APP_SLUG}..."
  APP_JSON=$(gh api "/apps/${APP_SLUG}")
  if [[ -z "${APP_ID}" ]]; then
    APP_ID=$(echo "${APP_JSON}" | jq -r '.id')
  fi

  echo "Fetching installation for ${APP_SLUG}..."
  INSTALL_JSON=$(gh api "/orgs/projectbluefin/installations" --jq ".installations[] | select(.app_slug == \"${APP_SLUG}\")")
  if [[ -z "${INSTALLATION_ID}" ]]; then
    INSTALLATION_ID=$(echo "${INSTALL_JSON}" | jq -r '.id')
  fi
fi

echo ""
echo "App ID:          ${APP_ID}"
echo "Installation ID: ${INSTALLATION_ID}"
echo "Namespace:       ${NAMESPACE}"
echo ""

# Resolve PEM path interactively only if not already provided.
if [[ -z "${PEM_PATH}" ]]; then
  echo "Download a new private key from:"
  echo "  https://github.com/organizations/projectbluefin/settings/apps/${APP_SLUG}"
  echo "Then choose Private keys -> Generate a private key. The .pem downloads once."
  echo ""

  read -rp "Path to the downloaded GitHub App private key (.pem file): " PEM_PATH
fi

PEM_PATH="${PEM_PATH/#\~/$HOME}"

if [[ ! -f "${PEM_PATH}" ]]; then
  echo "ERROR: File not found: ${PEM_PATH}" >&2
  exit 1
fi

if ! grep -qE 'BEGIN (RSA )?PRIVATE KEY' "${PEM_PATH}"; then
  echo "ERROR: ${PEM_PATH} does not look like a PEM private key." >&2
  echo "       Make sure you downloaded the .pem, not a token or fingerprint." >&2
  exit 1
fi

echo ""
echo "Applying secret ${SECRET_NAME} in namespace ${NAMESPACE}..."
kubectl create secret generic "${SECRET_NAME}" \
  --namespace "${NAMESPACE}" \
  --from-literal=github_app_id="${APP_ID}" \
  --from-literal=github_app_installation_id="${INSTALLATION_ID}" \
  --from-file=github_app_private_key="${PEM_PATH}" \
  --dry-run=client -o yaml | kubectl apply -f -

echo ""
echo "Done. Verify with:"
echo "  kubectl get secret ${SECRET_NAME} -n ${NAMESPACE}"
echo "  kubectl logs -n arc-systems deployment/arc-systems-gha-rs-controller --tail=20"
