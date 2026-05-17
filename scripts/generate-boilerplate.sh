#!/usr/bin/env bash

### Script to generate boilerplate for VMs

set -euo pipefail
shopt -s nocasematch

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATES_DIR="$SCRIPT_DIR/../templates"
VMS_DIR="$SCRIPT_DIR/../vms"

prompt_user() {
    local PROMPT="$1"

    while true; do
        read -rp "$PROMPT" ANSWER

        case "$ANSWER" in
            y | yes)
                echo ""
                return 0
                ;;
            n | no | "")
                echo ""
                return 1
                ;;
            e | exit)
                echo ""
                echo "Bye!"
                echo ""
                exit 0
                ;;
            *)
                echo ""
                echo "Invalid input; please enter 'y'/'yes', 'n'/'no', or 'e'/'exit'"
                echo ""
                ;;
        esac
    done
}

echo "This script generates the boilerplate for creating new VMs."

if prompt_user "Do you want to proceed? [y/N] (e to exit) "; then
    while true; do
        read -rp "Enter the name of the VM: " VMNAME

        if [[ -z "$VMNAME" ]]; then
            echo ""
            echo "Invalid input! Name must contain at least one character"
            echo ""
            continue
        fi

        if ! [[ "$VMNAME" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || (( ${#VMNAME} > 63 )); then
            echo ""
            echo "Invalid name! VM names must be lowercase alphanumeric and hyphens only,"
            echo "start/end with alphanumeric, and be at most 63 characters (RFC 1123)."
            echo ""
            continue
        fi

        if prompt_user "Is '$VMNAME' correct? [y/N] (e to exit) "; then
            TARGET_DIR="$VMS_DIR/$VMNAME"

            if [[ -d "$TARGET_DIR" ]] && [[ -n "$(ls -A "$TARGET_DIR" 2>/dev/null)" ]]; then
                echo ""
                echo "Warning: '$TARGET_DIR' already exists and is non-empty."
                if ! prompt_user "Overwrite existing files? [y/N] (e to exit) "; then
                    continue
                fi
            fi

            echo "Generating boilerplate..."
            echo ""

            mkdir -p "$TARGET_DIR"

            export VMNAME
            for TEMPLATE in "$TEMPLATES_DIR"/*.yaml; do
                FILENAME=$(basename "$TEMPLATE")
                envsubst '${VMNAME}' < "$TEMPLATE" > "$TARGET_DIR/$FILENAME"
            done

            echo "Done! Review and update the following VM-specific values before applying:"
            echo "  - Service port (11051) in service.yaml, networkpolicy.yaml, virtualmachine.yaml"
            echo "  - Disk size (20Gi) in pv.yaml and datavolume.yaml"
            echo "  - Node hostname (blizzard) in pv.yaml"
            echo "  - Migration annotations (IP, port) in virtualmachine.yaml"
            echo ""

            if prompt_user "Do you want to create more? [y/N] (e to exit) "; then
                continue
            else
                break
            fi
        fi
    done
fi
