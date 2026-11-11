#!/usr/bin/env bash
# Deploy the Zcastor vault to NEAR TESTNET and set up the desk's accounts.
#
#   near/deploy_testnet.sh zcastor-desk.testnet        # your testnet account
#
# Creates, all under your account (the OWNER):
#   vault.<you>      the contract
#   reporter.<you>   may post equity; can only make the desk stricter
#   btc.<you>, eth.<you>   agents. The vault only lets a registered agent call
#                    request_permit, settle and sign_anchor. (Swapping each
#                    agent's key for a function-call key limited to those three
#                    methods is the next step, after this deploy is confirmed.)
#
# Testnet only: no real funds. Safe to re-run; steps that are already done say
# so and the script carries on. Keys land in ~/.near-credentials/testnet/.
set -uo pipefail
cd "$(dirname "$0")/vault"

OWNER="${1:?usage: near/deploy_testnet.sh <your-account>.testnet}"
NEAR="${NEAR:-near}"
VAULT="vault.$OWNER"
REPORTER="reporter.$OWNER"
AGENTS=(btc eth)
SYMBOLS=(BTC ETH)
MPC="v1.signer-prod.testnet"
NET=(network-config testnet)
SIGN=(sign-with-legacy-keychain send)

# Limits, in US cents. From config/desk.json on a $10,000 paper desk:
# 2% total open risk, 0.5% a trade, 3.0x leverage, halt 75% under the day's start.
TOTAL_RISK=20000
TRADE_RISK=5000
LEVERAGE_X10=30
DEADMAN_BPS=7500
EQUITY=1000000
# Per agent: $100 risk budget, $5,000 notional.
AGENT_BUDGET=10000
AGENT_NOTIONAL=500000
# NVNM L1 testnet, with gas caps so a signature can never be made to burn gas.
EVM_CHAIN=787223
EVM_GAS_CAP=2000000
EVM_FEE_CAP=1000000000000

step() { printf '\n== %s\n' "$*"; }
call() {  # call <signer> <method> <json>
  "$NEAR" contract call-function as-transaction "$VAULT" "$2" json-args "$3" \
    prepaid-gas '50.0 Tgas' attached-deposit '0 NEAR' sign-as "$1" "${NET[@]}" "${SIGN[@]}"
}
view() { "$NEAR" contract call-function as-read-only "$VAULT" "$1" json-args "${2:-{\}}" "${NET[@]}" now; }
sub() {   # sub <name> <NEAR>
  "$NEAR" account create-account fund-myself "$1" "$2 NEAR" autogenerate-new-keypair \
    save-to-legacy-keychain sign-as "$OWNER" "${NET[@]}" "${SIGN[@]}" \
    || echo "   ($1 probably exists already; continuing)"
}

command -v "$NEAR" >/dev/null || { echo "near CLI not found. Install it:
  curl --proto '=https' --tlsv1.2 -LsSf https://github.com/near/near-cli-rs/releases/latest/download/near-cli-rs-installer.sh | sh"; exit 1; }

step "1/6 build the contract"
cargo near build non-reproducible-wasm || { echo "build failed"; exit 1; }
WASM="target/near/zcastor_vault.wasm"
ls -l "$WASM" || exit 1

step "2/6 accounts: $VAULT, $REPORTER"
sub "$VAULT" 5
sub "$REPORTER" 0.2

step "3/6 deploy and initialise"
INIT=$(printf '{"owner":"%s","reporter":"%s","mpc_contract":"%s","limits":{"total_risk_cents":%s,"per_trade_risk_cents":%s,"max_leverage_x10":%s,"deadman_bps":%s,"evm_chain_id":%s,"evm_gas_limit_cap":%s,"evm_max_fee_wei_cap":"%s"}}' \
  "$OWNER" "$REPORTER" "$MPC" "$TOTAL_RISK" "$TRADE_RISK" "$LEVERAGE_X10" "$DEADMAN_BPS" "$EVM_CHAIN" "$EVM_GAS_CAP" "$EVM_FEE_CAP")
"$NEAR" contract deploy "$VAULT" use-file "$WASM" with-init-call new json-args "$INIT" \
  prepaid-gas '100.0 Tgas' attached-deposit '0 NEAR' "${NET[@]}" "${SIGN[@]}" \
  || echo "   (already initialised? continuing)"

step "4/6 agents"
for i in "${!AGENTS[@]}"; do
  A="${AGENTS[$i]}.$OWNER"
  sub "$A" 0.5
  call "$OWNER" register_agent "$(printf '{"agent":"%s","symbol":"%s","risk_budget_cents":%s,"max_notional_cents":%s,"max_leverage_x10":%s}' \
    "$A" "${SYMBOLS[$i]}" "$AGENT_BUDGET" "$AGENT_NOTIONAL" "$LEVERAGE_X10")"
done

step "5/6 start the day at \$$((EQUITY / 100)) equity"
call "$OWNER" start_day "{\"equity_cents\":$EQUITY}"

step "6/6 what is on chain now"
view get_status
view get_limits
view list_agents

cat <<DONE

Deployed. Explorer: https://testnet.nearblocks.io/address/$VAULT

Try it (do X, see Y):
  # an agent asks for a \$40-risk trade: granted
  $NEAR contract call-function as-transaction $VAULT request_permit json-args '{"signal_hash":"demo-1","side":"Buy","notional_cents":100000,"risk_cents":4000,"leverage_x10":30}' prepaid-gas '30.0 Tgas' attached-deposit '0 NEAR' sign-as btc.$OWNER network-config testnet sign-with-legacy-keychain send
  # the same agent asks for \$90 of risk: refused, reason over_trade_risk
  $NEAR contract call-function as-transaction $VAULT request_permit json-args '{"signal_hash":"demo-2","side":"Buy","notional_cents":100000,"risk_cents":9000,"leverage_x10":30}' prepaid-gas '30.0 Tgas' attached-deposit '0 NEAR' sign-as btc.$OWNER network-config testnet sign-with-legacy-keychain send
  # both are on record
  $NEAR contract call-function as-read-only $VAULT list_decisions json-args '{"from":0,"limit":10}' network-config testnet now
DONE
