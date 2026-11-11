//! Zcastor desk vault.
//!
//! The limits an AI trading desk must stay inside, kept on NEAR instead of in
//! the desk's own config, so the software that wants to trade is not the
//! software that decides whether it may.
//!
//! Three jobs:
//!
//! 1. **Permits.** Before any fill, an agent calls `request_permit` with the
//!    trade's notional, risk and leverage. The vault checks them against the
//!    owner's limits and answers granted or refused, with the reason. Both
//!    outcomes are stored and logged. A refusal is a record, not an error.
//! 2. **Dead-man halt.** A reporter posts the desk's equity. If it has fallen
//!    past the owner's drawdown limit from the start of day, the vault halts and
//!    every permit is refused until the OWNER resumes. The reporter can only
//!    ever make the desk stricter.
//! 3. **A sandboxed signer.** Each agent's EVM address is derived by NEAR Chain
//!    Signatures from this contract's account and the agent's account id, so no
//!    private key for it exists anywhere. The vault builds the transaction
//!    itself and asks the MPC network to sign only if it is a zero-value call
//!    to the anchoring precompile with an allowed function selector, on the
//!    configured chain, under the gas caps. Nothing else can be signed with
//!    that key, whatever the agent asks for.
//!
//! Agents are NEAR accounts (sub-accounts of the desk) holding function-call
//! access keys limited to `request_permit`, `settle` and `sign_anchor` on this
//! contract. Such a key cannot transfer NEAR, deploy code or call anything else.
//!
//! Money amounts are US cents in `u64`. Leverage is tenths (30 = 3.0x).

use near_sdk::json_types::U128;
use near_sdk::store::{IterableMap, Vector};
use near_sdk::{
    env, near, require, AccountId, BorshStorageKey, Gas, NearToken, PanicOnDefault, Promise,
};

/// NVNM's anchoring module: 0x0000000000000000000000000000000000000A00.
const ANCHOR_ADDRESS: [u8; 20] = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0x0A, 0x00];
/// addRecord and updateRecordStatus, the only calls an agent's key may sign.
const DEFAULT_SELECTORS: [&str; 2] = ["64d25295", "97b40c25"];
/// Kept back from the gas forwarded to the MPC contract.
const SIGN_RESERVE: Gas = Gas::from_tgas(10);
const MAX_PAGE: u64 = 100;

#[near]
#[derive(BorshStorageKey)]
enum Key {
    Agents,
    Decisions,
}

#[near(serializers = [json, borsh])]
#[derive(Clone, Debug, PartialEq)]
pub struct Limits {
    /// Risk the whole desk may have open at once.
    pub total_risk_cents: u64,
    /// Risk any single trade may carry.
    pub per_trade_risk_cents: u64,
    /// Ceiling on every agent's leverage, in tenths.
    pub max_leverage_x10: u32,
    /// Halt when equity is this far below the start of day, in basis points.
    pub deadman_bps: u16,
    /// The only EVM chain `sign_anchor` will sign for.
    pub evm_chain_id: u64,
    pub evm_gas_limit_cap: u64,
    pub evm_max_fee_wei_cap: U128,
}

#[near(serializers = [json, borsh])]
#[derive(Clone, Debug, PartialEq)]
pub enum Side {
    Buy,
    Sell,
}

#[near(serializers = [json, borsh])]
#[derive(Clone, Debug)]
pub struct Agent {
    pub symbol: String,
    pub enabled: bool,
    pub risk_budget_cents: u64,
    pub max_notional_cents: u64,
    pub max_leverage_x10: u32,
    pub open_risk_cents: u64,
    pub permits: u64,
    pub refusals: u64,
    pub wins: u64,
    pub losses: u64,
    pub realised_pnl_cents: i64,
    pub anchors_signed: u64,
}

#[near(serializers = [json, borsh])]
#[derive(Clone, Debug)]
pub struct Decision {
    pub id: u64,
    pub agent: AccountId,
    pub symbol: String,
    /// The desk's own id for the signal (its origin hash), so this record can
    /// be matched to the anchored dossier.
    pub signal_hash: String,
    pub side: Side,
    pub notional_cents: u64,
    pub risk_cents: u64,
    pub leverage_x10: u32,
    pub granted: bool,
    pub reason: String,
    pub timestamp_ms: u64,
    pub settled: bool,
    pub pnl_cents: i64,
}

#[near(serializers = [json])]
pub struct Status {
    pub owner: AccountId,
    pub reporter: AccountId,
    pub mpc_contract: AccountId,
    pub halted: bool,
    pub halt_reason: String,
    pub open_risk_cents: u64,
    pub day_start_equity_cents: u64,
    pub last_equity_cents: u64,
    pub agents: u32,
    pub decisions: u64,
    pub granted: u64,
    pub refused: u64,
}

#[near(contract_state)]
#[derive(PanicOnDefault)]
pub struct Vault {
    owner: AccountId,
    reporter: AccountId,
    mpc_contract: AccountId,
    limits: Limits,
    selectors: Vec<String>,
    halted: bool,
    halt_reason: String,
    open_risk_cents: u64,
    day_start_equity_cents: u64,
    last_equity_cents: u64,
    granted: u64,
    refused: u64,
    agents: IterableMap<AccountId, Agent>,
    decisions: Vector<Decision>,
}

#[near]
impl Vault {
    #[init]
    pub fn new(
        owner: AccountId,
        reporter: AccountId,
        mpc_contract: AccountId,
        limits: Limits,
    ) -> Self {
        check_limits(&limits);
        Self {
            owner,
            reporter,
            mpc_contract,
            limits,
            selectors: DEFAULT_SELECTORS.iter().map(|s| s.to_string()).collect(),
            halted: false,
            halt_reason: String::new(),
            open_risk_cents: 0,
            day_start_equity_cents: 0,
            last_equity_cents: 0,
            granted: 0,
            refused: 0,
            agents: IterableMap::new(Key::Agents),
            decisions: Vector::new(Key::Decisions),
        }
    }

    // ── owner ────────────────────────────────────────────────────────────────

    pub fn set_limits(&mut self, limits: Limits) {
        self.only_owner();
        check_limits(&limits);
        event("limits_set", &format!("{{\"total_risk_cents\":{},\"per_trade_risk_cents\":{},\"max_leverage_x10\":{},\"deadman_bps\":{}}}",
            limits.total_risk_cents, limits.per_trade_risk_cents, limits.max_leverage_x10, limits.deadman_bps));
        self.limits = limits;
    }

    /// Add an agent or change its limits. Its counters and open risk are kept.
    pub fn register_agent(
        &mut self,
        agent: AccountId,
        symbol: String,
        risk_budget_cents: u64,
        max_notional_cents: u64,
        max_leverage_x10: u32,
    ) {
        self.only_owner();
        require!(!symbol.is_empty() && symbol.len() <= 24, "symbol must be 1-24 characters");
        require!(max_leverage_x10 >= 10, "leverage is in tenths; 10 is 1.0x");
        let previous = self.agents.get(&agent).cloned();
        let record = Agent {
            symbol: symbol.clone(),
            enabled: true,
            risk_budget_cents,
            max_notional_cents,
            max_leverage_x10,
            open_risk_cents: previous.as_ref().map_or(0, |a| a.open_risk_cents),
            permits: previous.as_ref().map_or(0, |a| a.permits),
            refusals: previous.as_ref().map_or(0, |a| a.refusals),
            wins: previous.as_ref().map_or(0, |a| a.wins),
            losses: previous.as_ref().map_or(0, |a| a.losses),
            realised_pnl_cents: previous.as_ref().map_or(0, |a| a.realised_pnl_cents),
            anchors_signed: previous.as_ref().map_or(0, |a| a.anchors_signed),
        };
        self.agents.insert(agent.clone(), record);
        event("agent_registered", &format!("{{\"agent\":\"{agent}\",\"symbol\":\"{symbol}\",\"risk_budget_cents\":{risk_budget_cents},\"max_notional_cents\":{max_notional_cents},\"max_leverage_x10\":{max_leverage_x10}}}"));
    }

    pub fn set_agent_enabled(&mut self, agent: AccountId, enabled: bool) {
        self.only_owner();
        let a = self.agents.get_mut(&agent).unwrap_or_else(|| env::panic_str("unknown agent"));
        a.enabled = enabled;
        event("agent_enabled", &format!("{{\"agent\":\"{agent}\",\"enabled\":{enabled}}}"));
    }

    /// Replace the function selectors `sign_anchor` accepts (8 hex characters each).
    pub fn set_selectors(&mut self, selectors: Vec<String>) {
        self.only_owner();
        for s in &selectors {
            require!(s.len() == 8 && hex::decode(s).is_ok(), "a selector is 8 hex characters");
        }
        self.selectors = selectors.iter().map(|s| s.to_lowercase()).collect();
    }

    pub fn set_reporter(&mut self, reporter: AccountId) {
        self.only_owner();
        self.reporter = reporter;
    }

    pub fn halt(&mut self, reason: String) {
        self.only_owner();
        self.do_halt(&format!("owner: {reason}"));
    }

    /// Only the owner can lift a halt, whoever caused it.
    pub fn resume(&mut self) {
        self.only_owner();
        self.halted = false;
        self.halt_reason.clear();
        event("resumed", "{}");
    }

    /// Set the equity the dead-man's drawdown is measured from.
    pub fn start_day(&mut self, equity_cents: u64) {
        self.only_owner();
        self.day_start_equity_cents = equity_cents;
        self.last_equity_cents = equity_cents;
        event("day_started", &format!("{{\"equity_cents\":{equity_cents}}}"));
    }

    // ── reporter ─────────────────────────────────────────────────────────────

    /// Post the desk's equity. Halts the desk if it has fallen past the limit.
    /// Returns whether the desk is halted afterwards. Never un-halts.
    pub fn report_equity(&mut self, equity_cents: u64) -> bool {
        let who = env::predecessor_account_id();
        require!(who == self.reporter || who == self.owner, "only the reporter or the owner");
        self.last_equity_cents = equity_cents;
        if !self.halted && self.day_start_equity_cents > 0 {
            let floor = (self.day_start_equity_cents as u128)
                * (10_000 - self.limits.deadman_bps as u128)
                / 10_000;
            if (equity_cents as u128) <= floor {
                self.do_halt(&format!(
                    "dead-man: equity {} at or below {} ({} bps under the day's start {})",
                    equity_cents, floor, self.limits.deadman_bps, self.day_start_equity_cents
                ));
            }
        }
        self.halted
    }

    // ── agents ───────────────────────────────────────────────────────────────

    /// Ask to open a trade. Always answers; a refusal is stored like a grant.
    pub fn request_permit(
        &mut self,
        signal_hash: String,
        side: Side,
        notional_cents: u64,
        risk_cents: u64,
        leverage_x10: u32,
    ) -> Decision {
        require!(signal_hash.len() <= 80, "signal_hash is at most 80 characters");
        let agent_id = env::predecessor_account_id();
        let agent = self
            .agents
            .get(&agent_id)
            .cloned()
            .unwrap_or_else(|| env::panic_str("caller is not a registered agent"));

        let reason = self.refusal_reason(&agent, notional_cents, risk_cents, leverage_x10);
        let granted = reason.is_none();
        let decision = Decision {
            id: self.decisions.len() as u64,
            agent: agent_id.clone(),
            symbol: agent.symbol.clone(),
            signal_hash,
            side,
            notional_cents,
            risk_cents,
            leverage_x10,
            granted,
            reason: reason.unwrap_or("within_limits").to_string(),
            timestamp_ms: env::block_timestamp_ms(),
            settled: !granted,
            pnl_cents: 0,
        };

        let a = self.agents.get_mut(&agent_id).unwrap();
        if granted {
            a.permits += 1;
            a.open_risk_cents += risk_cents;
            self.open_risk_cents += risk_cents;
            self.granted += 1;
        } else {
            a.refusals += 1;
            self.refused += 1;
        }
        event(
            if granted { "permit_granted" } else { "permit_refused" },
            &format!(
                "{{\"id\":{},\"agent\":\"{}\",\"symbol\":\"{}\",\"signal_hash\":\"{}\",\"notional_cents\":{},\"risk_cents\":{},\"leverage_x10\":{},\"reason\":\"{}\"}}",
                decision.id, decision.agent, decision.symbol, escape(&decision.signal_hash),
                notional_cents, risk_cents, leverage_x10, decision.reason
            ),
        );
        self.decisions.push(decision.clone());
        decision
    }

    /// Close a granted permit: frees its risk and records the result.
    pub fn settle(&mut self, permit_id: u64, pnl_cents: i64) {
        let agent_id = env::predecessor_account_id();
        let d = self
            .decisions
            .get_mut(permit_id as u32)
            .unwrap_or_else(|| env::panic_str("unknown permit"));
        require!(d.agent == agent_id, "only the agent that holds this permit");
        require!(d.granted && !d.settled, "permit is not open");
        d.settled = true;
        d.pnl_cents = pnl_cents;
        let risk = d.risk_cents;

        let a = self.agents.get_mut(&agent_id).unwrap();
        a.open_risk_cents = a.open_risk_cents.saturating_sub(risk);
        a.realised_pnl_cents = a.realised_pnl_cents.saturating_add(pnl_cents);
        if pnl_cents > 0 {
            a.wins += 1;
        } else if pnl_cents < 0 {
            a.losses += 1;
        }
        self.open_risk_cents = self.open_risk_cents.saturating_sub(risk);
        event("permit_settled", &format!("{{\"id\":{permit_id},\"agent\":\"{agent_id}\",\"pnl_cents\":{pnl_cents}}}"));
    }

    /// Sign one anchoring transaction with the agent's derived key.
    ///
    /// The agent supplies only the fields that vary. The vault fixes the
    /// destination (the anchoring precompile), the value (zero) and the chain,
    /// checks the function selector and the gas caps, builds the EIP-1559
    /// signing hash itself, and forwards exactly that hash to the MPC contract
    /// with the agent's account id as the derivation path. Returns the MPC
    /// contract's signature.
    pub fn sign_anchor(
        &mut self,
        nonce: u64,
        max_priority_fee_wei: U128,
        max_fee_wei: U128,
        gas_limit: u64,
        data_hex: String,
    ) -> Promise {
        let agent_id = env::predecessor_account_id();
        let a = self
            .agents
            .get_mut(&agent_id)
            .unwrap_or_else(|| env::panic_str("caller is not a registered agent"));
        require!(a.enabled, "agent is disabled");
        a.anchors_signed += 1;

        let hash = self.anchor_tx_hash(nonce, max_priority_fee_wei, max_fee_wei, gas_limit, data_hex);
        event("anchor_signing", &format!("{{\"agent\":\"{agent_id}\",\"nonce\":{nonce},\"tx_hash_to_sign\":\"{hash}\"}}"));

        let args = format!(
            "{{\"request\":{{\"payload_v2\":{{\"Ecdsa\":\"{hash}\"}},\"path\":\"{agent_id}\",\"domain_id\":0}}}}"
        );
        let gas = env::prepaid_gas()
            .saturating_sub(env::used_gas())
            .saturating_sub(SIGN_RESERVE);
        Promise::new(self.mpc_contract.clone()).function_call(
            "sign".to_string(),
            args.into_bytes(),
            NearToken::from_yoctonear(1),
            gas,
        )
    }

    // ── views ────────────────────────────────────────────────────────────────

    /// The hash `sign_anchor` would sign, or a panic naming the rule it breaks.
    /// A view, so anyone can check what the vault will and will not sign.
    pub fn anchor_tx_hash(
        &self,
        nonce: u64,
        max_priority_fee_wei: U128,
        max_fee_wei: U128,
        gas_limit: u64,
        data_hex: String,
    ) -> String {
        let data = hex::decode(data_hex.trim_start_matches("0x"))
            .unwrap_or_else(|_| env::panic_str("data_hex is not hex"));
        require!(data.len() >= 4, "calldata has no function selector");
        let selector = hex::encode(&data[..4]);
        require!(
            self.selectors.contains(&selector),
            "refused: the function selector is not one the vault signs"
        );
        require!(gas_limit <= self.limits.evm_gas_limit_cap, "refused: gas limit over the cap");
        require!(
            max_fee_wei.0 <= self.limits.evm_max_fee_wei_cap.0,
            "refused: max fee over the cap"
        );
        require!(max_priority_fee_wei.0 <= max_fee_wei.0, "priority fee exceeds max fee");
        hex::encode(eip1559_signing_hash(
            self.limits.evm_chain_id,
            nonce,
            max_priority_fee_wei.0,
            max_fee_wei.0,
            gas_limit,
            &ANCHOR_ADDRESS,
            &data,
        ))
    }

    pub fn get_status(&self) -> Status {
        Status {
            owner: self.owner.clone(),
            reporter: self.reporter.clone(),
            mpc_contract: self.mpc_contract.clone(),
            halted: self.halted,
            halt_reason: self.halt_reason.clone(),
            open_risk_cents: self.open_risk_cents,
            day_start_equity_cents: self.day_start_equity_cents,
            last_equity_cents: self.last_equity_cents,
            agents: self.agents.len(),
            decisions: self.decisions.len() as u64,
            granted: self.granted,
            refused: self.refused,
        }
    }

    pub fn get_limits(&self) -> Limits {
        self.limits.clone()
    }

    pub fn get_selectors(&self) -> Vec<String> {
        self.selectors.clone()
    }

    pub fn get_agent(&self, agent: AccountId) -> Option<Agent> {
        self.agents.get(&agent).cloned()
    }

    pub fn list_agents(&self) -> Vec<(AccountId, Agent)> {
        self.agents.iter().map(|(k, v)| (k.clone(), v.clone())).collect()
    }

    pub fn get_decision(&self, id: u64) -> Option<Decision> {
        self.decisions.get(id as u32).cloned()
    }

    /// Decisions from `from`, oldest first, at most 100 per call.
    pub fn list_decisions(&self, from: u64, limit: u64) -> Vec<Decision> {
        let end = (from + limit.min(MAX_PAGE)).min(self.decisions.len() as u64);
        (from..end).filter_map(|i| self.decisions.get(i as u32).cloned()).collect()
    }
}

impl Vault {
    fn only_owner(&self) {
        require!(env::predecessor_account_id() == self.owner, "only the owner");
    }

    fn do_halt(&mut self, reason: &str) {
        self.halted = true;
        self.halt_reason = reason.to_string();
        event("halted", &format!("{{\"reason\":\"{}\"}}", escape(reason)));
    }

    /// Why a trade is refused, in the order a reader would check; None if allowed.
    fn refusal_reason(&self, agent: &Agent, notional: u64, risk: u64, leverage: u32) -> Option<&'static str> {
        if self.halted {
            return Some("desk_halted");
        }
        if !agent.enabled {
            return Some("agent_disabled");
        }
        if risk == 0 || notional == 0 {
            return Some("zero_size");
        }
        if leverage > agent.max_leverage_x10 || leverage > self.limits.max_leverage_x10 {
            return Some("over_leverage");
        }
        if notional > agent.max_notional_cents {
            return Some("over_agent_notional");
        }
        if risk > self.limits.per_trade_risk_cents {
            return Some("over_trade_risk");
        }
        if agent.open_risk_cents.saturating_add(risk) > agent.risk_budget_cents {
            return Some("over_agent_budget");
        }
        if self.open_risk_cents.saturating_add(risk) > self.limits.total_risk_cents {
            return Some("over_desk_budget");
        }
        None
    }
}

fn check_limits(l: &Limits) {
    require!(l.deadman_bps > 0 && l.deadman_bps < 10_000, "deadman_bps is between 1 and 9999");
    require!(l.max_leverage_x10 >= 10, "leverage is in tenths; 10 is 1.0x");
    require!(l.per_trade_risk_cents <= l.total_risk_cents, "per-trade risk exceeds the desk budget");
    require!(l.evm_chain_id > 0, "evm_chain_id is required");
}

fn event(name: &str, data: &str) {
    env::log_str(&format!(
        "EVENT_JSON:{{\"standard\":\"zcastor-vault\",\"version\":\"1.0.0\",\"event\":\"{name}\",\"data\":[{data}]}}"
    ));
}

fn escape(s: &str) -> String {
    s.chars()
        .filter(|c| !c.is_control())
        .flat_map(|c| match c {
            '"' => vec!['\\', '"'],
            '\\' => vec!['\\', '\\'],
            c => vec![c],
        })
        .collect()
}

// ── EIP-1559 signing hash ────────────────────────────────────────────────────
// keccak256(0x02 || rlp([chain_id, nonce, max_priority_fee, max_fee, gas_limit,
//                        to, value = 0, data, access_list = []]))

pub fn eip1559_signing_hash(
    chain_id: u64,
    nonce: u64,
    max_priority_fee: u128,
    max_fee: u128,
    gas_limit: u64,
    to: &[u8; 20],
    data: &[u8],
) -> Vec<u8> {
    let items = [
        rlp_uint(chain_id as u128),
        rlp_uint(nonce as u128),
        rlp_uint(max_priority_fee),
        rlp_uint(max_fee),
        rlp_uint(gas_limit as u128),
        rlp_bytes(to),
        rlp_uint(0),
        rlp_bytes(data),
        rlp_list(&[]),
    ];
    let mut out = vec![0x02];
    out.extend(rlp_list(&items));
    env::keccak256(&out)
}

fn rlp_length(base: u8, len: usize) -> Vec<u8> {
    if len < 56 {
        vec![base + len as u8]
    } else {
        let be: Vec<u8> = len.to_be_bytes().iter().copied().skip_while(|b| *b == 0).collect();
        let mut out = vec![base + 55 + be.len() as u8];
        out.extend(be);
        out
    }
}

fn rlp_bytes(b: &[u8]) -> Vec<u8> {
    if b.len() == 1 && b[0] < 0x80 {
        return vec![b[0]];
    }
    let mut out = rlp_length(0x80, b.len());
    out.extend_from_slice(b);
    out
}

fn rlp_uint(v: u128) -> Vec<u8> {
    let be: Vec<u8> = v.to_be_bytes().iter().copied().skip_while(|b| *b == 0).collect();
    rlp_bytes(&be)
}

fn rlp_list(items: &[Vec<u8>]) -> Vec<u8> {
    let total: usize = items.iter().map(|i| i.len()).sum();
    let mut out = rlp_length(0xc0, total);
    for i in items {
        out.extend_from_slice(i);
    }
    out
}

#[cfg(test)]
mod tests;
