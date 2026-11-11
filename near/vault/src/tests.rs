use super::*;
use near_sdk::test_utils::{get_logs, VMContextBuilder};
use near_sdk::testing_env;

fn acc(s: &str) -> AccountId {
    s.parse().unwrap()
}

fn as_caller(who: &str) {
    testing_env!(VMContextBuilder::new()
        .predecessor_account_id(acc(who))
        .prepaid_gas(Gas::from_tgas(300))
        .build());
}

fn limits() -> Limits {
    Limits {
        total_risk_cents: 20_000,      // $200 open across the desk
        per_trade_risk_cents: 5_000,   // $50 a trade
        max_leverage_x10: 30,          // 3.0x
        deadman_bps: 7_500,            // halt 75% under the day's start
        evm_chain_id: 787_223,
        evm_gas_limit_cap: 2_000_000,
        evm_max_fee_wei_cap: U128(1u128 << 71),
    }
}

/// A vault with BTC and ETH agents, each with a $100 risk budget.
fn vault() -> Vault {
    as_caller("owner.testnet");
    let mut v = Vault::new(acc("owner.testnet"), acc("reporter.testnet"),
                           acc("v1.signer-prod.testnet"), limits());
    v.register_agent(acc("btc.desk.testnet"), "BTC".into(), 10_000, 500_000, 30);
    v.register_agent(acc("eth.desk.testnet"), "ETH".into(), 10_000, 300_000, 20);
    v.start_day(1_000_000);
    v
}

fn ask(v: &mut Vault, who: &str, notional: u64, risk: u64, lev: u32) -> Decision {
    as_caller(who);
    v.request_permit("sha256:abc".into(), Side::Buy, notional, risk, lev)
}

// ── permits ──────────────────────────────────────────────────────────────────

#[test]
fn a_trade_inside_every_limit_is_granted_and_its_risk_is_held() {
    let mut v = vault();
    let d = ask(&mut v, "btc.desk.testnet", 100_000, 4_000, 30);
    assert!(d.granted && d.reason == "within_limits" && !d.settled);
    assert_eq!(v.get_status().open_risk_cents, 4_000);
    assert_eq!(v.get_agent(acc("btc.desk.testnet")).unwrap().open_risk_cents, 4_000);
}

#[test]
fn each_limit_refuses_with_its_own_reason() {
    let mut v = vault();
    assert_eq!(ask(&mut v, "btc.desk.testnet", 100_000, 4_000, 31).reason, "over_leverage");
    assert_eq!(ask(&mut v, "eth.desk.testnet", 100_000, 4_000, 25).reason, "over_leverage");
    assert_eq!(ask(&mut v, "btc.desk.testnet", 500_001, 4_000, 10).reason, "over_agent_notional");
    assert_eq!(ask(&mut v, "btc.desk.testnet", 100_000, 5_001, 10).reason, "over_trade_risk");
    assert_eq!(ask(&mut v, "btc.desk.testnet", 100_000, 0, 10).reason, "zero_size");
    let s = v.get_status();
    assert_eq!((s.granted, s.refused, s.open_risk_cents), (0, 5, 0));
}

#[test]
fn an_agent_cannot_exceed_its_own_budget() {
    let mut v = vault();
    assert!(ask(&mut v, "btc.desk.testnet", 100_000, 5_000, 10).granted);
    assert!(ask(&mut v, "btc.desk.testnet", 100_000, 5_000, 10).granted);
    let d = ask(&mut v, "btc.desk.testnet", 100_000, 1, 10);
    assert!(!d.granted && d.reason == "over_agent_budget");
}

#[test]
fn agents_share_one_desk_budget() {
    let mut v = vault();
    as_caller("owner.testnet");
    v.register_agent(acc("sol.desk.testnet"), "SOL".into(), 10_000, 300_000, 20);
    for who in ["btc.desk.testnet", "eth.desk.testnet"] {
        assert!(ask(&mut v, who, 100_000, 5_000, 10).granted);
        assert!(ask(&mut v, who, 100_000, 5_000, 10).granted);
    }
    // $200 is open. SOL has its whole budget left, but the desk has none.
    let d = ask(&mut v, "sol.desk.testnet", 100_000, 1_000, 10);
    assert!(!d.granted && d.reason == "over_desk_budget");
}

#[test]
fn settling_frees_the_risk_and_scores_the_agent() {
    let mut v = vault();
    let a = ask(&mut v, "btc.desk.testnet", 100_000, 5_000, 10);
    let b = ask(&mut v, "btc.desk.testnet", 100_000, 5_000, 10);
    as_caller("btc.desk.testnet");
    v.settle(a.id, 7_500);
    v.settle(b.id, -5_000);
    let agent = v.get_agent(acc("btc.desk.testnet")).unwrap();
    assert_eq!((agent.open_risk_cents, agent.wins, agent.losses, agent.realised_pnl_cents),
               (0, 1, 1, 2_500));
    assert_eq!(v.get_status().open_risk_cents, 0);
    assert!(ask(&mut v, "btc.desk.testnet", 100_000, 5_000, 10).granted);
}

#[test]
#[should_panic(expected = "only the agent that holds this permit")]
fn an_agent_cannot_settle_another_agents_permit() {
    let mut v = vault();
    let d = ask(&mut v, "btc.desk.testnet", 100_000, 5_000, 10);
    as_caller("eth.desk.testnet");
    v.settle(d.id, 1_000_000);
}

#[test]
#[should_panic(expected = "permit is not open")]
fn a_permit_settles_once() {
    let mut v = vault();
    let d = ask(&mut v, "btc.desk.testnet", 100_000, 5_000, 10);
    as_caller("btc.desk.testnet");
    v.settle(d.id, 100);
    v.settle(d.id, 100);
}

#[test]
#[should_panic(expected = "caller is not a registered agent")]
fn a_stranger_gets_no_permit() {
    let mut v = vault();
    ask(&mut v, "mallory.testnet", 1, 1, 10);
}

#[test]
fn a_disabled_agent_is_refused_and_the_refusal_is_recorded() {
    let mut v = vault();
    as_caller("owner.testnet");
    v.set_agent_enabled(acc("eth.desk.testnet"), false);
    let d = ask(&mut v, "eth.desk.testnet", 100_000, 1_000, 10);
    assert_eq!(d.reason, "agent_disabled");
    assert_eq!(v.get_agent(acc("eth.desk.testnet")).unwrap().refusals, 1);
    assert_eq!(v.list_decisions(0, 10).len(), 1);
}

#[test]
fn every_decision_is_logged_as_an_event() {
    let mut v = vault();
    ask(&mut v, "btc.desk.testnet", 100_000, 4_000, 30);
    assert!(get_logs().iter().any(|l| l.contains("\"event\":\"permit_granted\"")
        && l.contains("\"symbol\":\"BTC\"")));
    ask(&mut v, "btc.desk.testnet", 100_000, 9_000, 30);
    assert!(get_logs().iter().any(|l| l.contains("\"event\":\"permit_refused\"")
        && l.contains("over_trade_risk")));
}

// ── owner-only and the dead-man ──────────────────────────────────────────────

#[test]
#[should_panic(expected = "only the owner")]
fn an_agent_cannot_raise_its_own_limits() {
    let mut v = vault();
    as_caller("btc.desk.testnet");
    v.register_agent(acc("btc.desk.testnet"), "BTC".into(), u64::MAX, u64::MAX, 1_000);
}

#[test]
#[should_panic(expected = "only the owner")]
fn an_agent_cannot_change_the_desk_limits() {
    let mut v = vault();
    as_caller("btc.desk.testnet");
    v.set_limits(Limits { total_risk_cents: u64::MAX, ..limits() });
}

#[test]
fn the_dead_man_halts_at_the_drawdown_limit_and_not_before() {
    let mut v = vault();
    as_caller("reporter.testnet");
    assert!(!v.report_equity(250_001));          // 74.9999% down: still trading
    assert!(v.report_equity(250_000));           // 75% down: halted
    assert!(v.get_status().halt_reason.starts_with("dead-man"));
    let d = ask(&mut v, "btc.desk.testnet", 100_000, 1_000, 10);
    assert!(!d.granted && d.reason == "desk_halted");
}

#[test]
fn a_recovery_in_equity_does_not_lift_the_halt() {
    let mut v = vault();
    as_caller("reporter.testnet");
    v.report_equity(100_000);
    assert!(v.report_equity(2_000_000), "only the owner un-halts");
}

#[test]
#[should_panic(expected = "only the owner")]
fn the_reporter_cannot_resume() {
    let mut v = vault();
    as_caller("reporter.testnet");
    v.report_equity(1);
    v.resume();
}

#[test]
#[should_panic(expected = "only the reporter or the owner")]
fn an_agent_cannot_report_equity() {
    let mut v = vault();
    as_caller("btc.desk.testnet");
    v.report_equity(9_999_999);
}

#[test]
fn the_owner_resumes_and_trading_continues() {
    let mut v = vault();
    as_caller("owner.testnet");
    v.halt("maintenance".into());
    assert_eq!(ask(&mut v, "btc.desk.testnet", 100_000, 1_000, 10).reason, "desk_halted");
    as_caller("owner.testnet");
    v.resume();
    assert!(ask(&mut v, "btc.desk.testnet", 100_000, 1_000, 10).granted);
}

#[test]
fn re_registering_an_agent_keeps_its_open_risk_and_record() {
    let mut v = vault();
    ask(&mut v, "btc.desk.testnet", 100_000, 5_000, 10);
    as_caller("owner.testnet");
    v.register_agent(acc("btc.desk.testnet"), "BTC".into(), 6_000, 500_000, 30);
    let a = v.get_agent(acc("btc.desk.testnet")).unwrap();
    assert_eq!((a.open_risk_cents, a.permits, a.risk_budget_cents), (5_000, 1, 6_000));
}

// ── the sandboxed signer ─────────────────────────────────────────────────────
// Reference hashes come from eth-account 0.14 (checked there by recovering the
// signer from a signature over each hash).

fn hash(v: &Vault, nonce: u64, tip: u128, fee: u128, gas: u64, data: &str) -> String {
    v.anchor_tx_hash(nonce, U128(tip), U128(fee), gas, data.to_string())
}

#[test]
fn the_signing_hash_matches_ethereums_for_short_and_long_calldata() {
    let v = vault();
    assert_eq!(hash(&v, 0, 0, 1, 21_000, &format!("0x64d25295{}", "00".repeat(4))),
               "cb596e07a89c3c8340317c8a3b0aeee90707374eb39c97b8350b89d6cc1d76ae");
    assert_eq!(hash(&v, 7, 1_500_000_000, 30_000_000_000, 400_000,
                    &format!("0x97b40c25{}", "ab".repeat(300))),
               "a400718705296b4a1e8a6b89ebdc243f090c31e420810c7e6de78deb5380b85a");
    assert_eq!(hash(&v, 300, 1, 1u128 << 70, 2_000_000, "0x64d252957f"),
               "57be6bfa62890759ac0cf45b8cbae5b996d91ea93df2311fbee079a14f6b1492");
}

#[test]
#[should_panic(expected = "the function selector is not one the vault signs")]
fn it_will_not_sign_a_token_transfer() {
    let v = vault();
    // ERC-20 transfer(address,uint256)
    hash(&v, 0, 0, 1, 60_000, &format!("0xa9059cbb{}", "00".repeat(64)));
}

#[test]
#[should_panic(expected = "the function selector is not one the vault signs")]
fn it_will_not_sign_grant_role_on_the_anchor_itself() {
    let v = vault();
    hash(&v, 0, 0, 1, 60_000, &format!("0x2f2ff15d{}", "00".repeat(64)));
}

#[test]
#[should_panic(expected = "calldata has no function selector")]
fn it_will_not_sign_a_plain_value_transfer() {
    let v = vault();
    hash(&v, 0, 0, 1, 21_000, "0x");
}

#[test]
#[should_panic(expected = "gas limit over the cap")]
fn it_will_not_sign_a_gas_draining_limit() {
    let v = vault();
    hash(&v, 0, 0, 1, 2_000_001, "0x64d2529500");
}

#[test]
#[should_panic(expected = "max fee over the cap")]
fn it_will_not_sign_a_gas_draining_fee() {
    let v = vault();
    hash(&v, 0, 0, (1u128 << 71) + 1, 100_000, "0x64d2529500");
}

#[test]
#[should_panic(expected = "caller is not a registered agent")]
fn a_stranger_cannot_use_the_signer() {
    let mut v = vault();
    as_caller("mallory.testnet");
    let _ = v.sign_anchor(0, U128(0), U128(1), 21_000, "0x64d2529500".into());
}

#[test]
#[should_panic(expected = "the function selector is not one the vault signs")]
fn a_registered_agent_still_cannot_sign_outside_the_allowlist() {
    let mut v = vault();
    as_caller("btc.desk.testnet");
    let _ = v.sign_anchor(0, U128(0), U128(1), 60_000, format!("0xa9059cbb{}", "00".repeat(64)));
}

#[test]
fn an_allowed_call_is_forwarded_to_the_mpc_contract_with_the_agents_path() {
    let mut v = vault();
    as_caller("btc.desk.testnet");
    let _ = v.sign_anchor(7, U128(1_500_000_000), U128(30_000_000_000), 400_000,
                  format!("0x97b40c25{}", "ab".repeat(300)));
    assert!(get_logs().iter().any(|l| l.contains("anchor_signing")
        && l.contains("a400718705296b4a1e8a6b89ebdc243f090c31e420810c7e6de78deb5380b85a")));
    let receipts = near_sdk::test_utils::get_created_receipts();
    assert_eq!(receipts.len(), 1);
    assert_eq!(receipts[0].receiver_id, acc("v1.signer-prod.testnet"));
    assert_eq!(v.get_agent(acc("btc.desk.testnet")).unwrap().anchors_signed, 1);
}

#[test]
fn the_owner_can_change_the_allowlist() {
    let mut v = vault();
    as_caller("owner.testnet");
    v.set_selectors(vec!["64D25295".into()]);
    assert_eq!(v.get_selectors(), vec!["64d25295".to_string()]);
}
