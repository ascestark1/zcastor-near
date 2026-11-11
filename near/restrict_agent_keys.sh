#!/usr/bin/env bash
# Narrow an agent account to a function-call key, in two steps you run by hand.
#
#   near/restrict_agent_keys.sh add  <owner>.testnet btc   # step 1: add the narrow key
#   near/restrict_agent_keys.sh list <owner>.testnet btc   # look at the keys on chain
#   near/restrict_agent_keys.sh drop <owner>.testnet btc <full-access-public-key>
#
# After step 1 the agent has two keys: its original full-access key and a new
# key that can only call request_permit, settle and sign_anchor on the vault.
# Step 1 can't lock anything out. `drop` deletes the full-access key and can't
# be undone: after it the account can never deploy code, move NEAR or add keys.
# Run a permit request between the two steps and check it still works.
#
# The near CLI signs with ~/.near-credentials/<network>/<account>.json, and
# keeps a second copy of every key in a folder of the same name. To test the
# narrow key on its own before `drop`, back up both copies of the full-access
# key, copy the new key's file over <account>.json, and request a permit: the
# "signed successfully" line must show the new key. After `drop`, leave the
# narrow key as <account>.json.
#
# sign_anchor attaches no deposit (the vault pays the signer's 1 yocto), so a
# function-call key, which can't attach deposits, is enough for all three.
set -euo pipefail
cmd="${1:?add | list | drop}"; owner="${2:?owner account}"; name="${3:?agent name, e.g. btc}"
agent="$name.$owner"; vault="vault.$owner"; net="${NEAR_NETWORK:-testnet}"
methods='request_permit, settle, sign_anchor'

case "$cmd" in
  list)
    near account list-keys "$agent" network-config "$net" now ;;
  add)
    # Flag names changed between near-cli-rs releases; try the current ones first.
    near account add-key "$agent" grant-function-call-access --allowance 'unlimited' \
        --contract-account-id "$vault" --function-names "$methods" \
        autogenerate-new-keypair save-to-legacy-keychain \
        network-config "$net" sign-with-legacy-keychain send \
    || near account add-key "$agent" grant-function-call-access --allowance 'unlimited' \
        --receiver-account-id "$vault" --method-names "$methods" \
        autogenerate-new-keypair save-to-legacy-keychain \
        network-config "$net" sign-with-legacy-keychain send
    echo; echo "Keys now on $agent:"
    near account list-keys "$agent" network-config "$net" now ;;
  drop)
    key="${4:?the full-access public key to delete (ed25519:...), from 'list'}"
    read -r -p "Delete full-access key $key from $agent? This can't be undone. Type the agent name to confirm: " ok
    [ "$ok" = "$name" ] || { echo "not confirmed"; exit 1; }
    near account delete-keys "$agent" public-keys "$key" \
        network-config "$net" sign-with-legacy-keychain send
    near account list-keys "$agent" network-config "$net" now ;;
  *) echo "add | list | drop"; exit 1 ;;
esac
