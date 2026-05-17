#!/usr/bin/env bash

### Script to generate boilerplate for VMs

shopt -s nocasematch

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

        if prompt_user "Is '$VMNAME' correct? [y/N] (e to exit) "; then
            echo "Generating boilerplate..."
            echo ""

            mkdir -p "../vms/$VMNAME"

            export VMNAME
            for TEMPLATE in ../templates/*.yaml; do
                FILENAME=$(basename "$TEMPLATE")
                envsubst < "$TEMPLATE" > ../vms/"$VMNAME"/"$FILENAME"
            done

            if prompt_user "Done! Do you want to create more? [y/N] (e to exit) "; then
                continue
            else
                break
            fi
        fi
    done
fi
