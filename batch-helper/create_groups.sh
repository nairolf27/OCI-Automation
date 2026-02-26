#!/bin/bash
# =============================================================================
# Script : create_groups.sh
# Usage  : ./create_groups.sh <domain_name> <domain_id>
# =============================================================================

set -euo pipefail

if [ $# -ne 2 ]; then
  echo "Usage : $0 <domain_name> <domain_id>"
  exit 1
fi

DOMAIN_NAME="$1"
DOMAIN_ID="$2"

# --- Tenancy ID ---
TENANCY_ID=$(oci iam compartment list --include-root \
  --query 'data[?"compartment-id"==null] | [0].id' \
  --raw-output 2>/dev/null | tr -d '[]" \n')

[ -z "${TENANCY_ID}" ] && echo "[ERREUR] Impossible de récupérer le Tenancy ID." && exit 1

# --- URL du domaine ---
DOMAIN_URL=$(oci iam domain list \
  --compartment-id "${TENANCY_ID}" --all \
  --query "data[?\"display-name\"=='${DOMAIN_NAME}'].url | [0]" \
  --raw-output 2>/dev/null || true)

if [ -z "${DOMAIN_URL}" ] || [ "${DOMAIN_URL}" = "None" ] || [ "${DOMAIN_URL}" = "null" ]; then
  echo "[ERREUR] Domaine '${DOMAIN_NAME}' introuvable."
  exit 1
fi

# --- Groupes à créer ---
GROUPS_NAMES=(
  "admin_${DOMAIN_ID}_tata"
  "rw_${DOMAIN_ID}_tata"
  "ro_${DOMAIN_ID}_tata"
  "admin_${DOMAIN_ID}_tato"
  "rw_${DOMAIN_ID}_tato"
  "ro_${DOMAIN_ID}_tato"
)

echo "Domaine : ${DOMAIN_NAME} | Endpoint : ${DOMAIN_URL}"
echo ""

SUCCESS=0
FAILED=0

for GROUP_NAME in "${GROUPS_NAMES[@]}"; do
  if oci identity-domains group create \
      --endpoint "${DOMAIN_URL}" \
      --display-name "${GROUP_NAME}" \
      --schemas '["urn:ietf:params:scim:schemas:core:2.0:Group"]' \
      --output table > /dev/null 2>&1; then
    echo "[OK]     ${GROUP_NAME}"
    SUCCESS=$(( SUCCESS + 1 ))
  else
    echo "[ERREUR] ${GROUP_NAME}"
    FAILED=$(( FAILED + 1 ))
  fi
done

echo ""
echo "Résultat : ${SUCCESS} créé(s), ${FAILED} en erreur."

[ "${FAILED}" -gt 0 ] && exit 1
exit 0
