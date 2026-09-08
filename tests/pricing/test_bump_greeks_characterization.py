"""Characterization test for AmericanCRR.greeks(): bit-exact golden values.

The bump-and-revalue suite in ``pricing.engine._bump_greeks`` feeds the drift
canary, so a shared-bump performance refactor is only allowed to land if every
output float is identical. The existing suites pin the tree loosely (rel=5e-3
against BSM); this test pins the *current* outputs exactly, via ``float.hex()``,
so any refactor that changes one bit anywhere fails loudly.

Golden values were generated from the pre-refactor implementation on
2026-09-07 (43 tree evaluations per greeks() call).
"""

from __future__ import annotations

import pytest

from pricing.conventions import GREEK_NAMES, GreeksConventions
from pricing.engine import AmericanCRR

CONV = GreeksConventions(
    vega_unit="per_1.00",
    theta_unit="per_year",
    delta_kind="spot",
    gamma_kind="spot",
)
AMER = AmericanCRR(n_steps=401)

# (S, K, T, r, sigma, call_put, q) — SPY-flavoured representative contracts,
# plus a q=0 ATM put (early-exercise premium) and a deep-OTM zero-price put
# (elasticity's copysign(inf) branch).
CASES = [
    (592.31, 590.0, 32 / 365, 0.043, 0.185, "call", 0.013),  # ATM call
    (592.31, 590.0, 32 / 365, 0.043, 0.185, "put", 0.013),  # ATM put
    (592.31, 500.0, 7 / 365, 0.043, 0.22, "put", 0.013),  # short OTM put
    (592.31, 650.0, 180 / 365, 0.043, 0.19, "call", 0.013),  # long OTM call
    (592.31, 550.0, 1.0, 0.043, 0.20, "call", 0.013),  # 1y ITM call
    (100.0, 100.0, 0.5, 0.05, 0.20, "put", 0.0),  # q=0 ATM put
    (592.31, 300.0, 3 / 365, 0.043, 0.30, "put", 0.013),  # zero-price branch
]

GOLDEN: list[dict[str, str]] = [
    {
        "price": "0x1.dd0882aa61b94p+3",
        "delta": "0x1.234af8cae8829p-1",
        "dual_delta": "-0x1.177f31898129fp-1",
        "vega": "0x1.140cb75d43ab6p+6",
        "theta": "-0x1.4855bca5b1e27p+6",
        "rho": "0x1.ba6eb332a5387p+4",
        "rho_dividend": "-0x1.cf57ed8711f37p+4",
        "gamma": "-0x1.761c924163ca0p-37",
        "dual_gamma": "-0x1.1f4615ea60edap-41",
        "vanna": "-0x1.66a42c18b4544p-5",
        "volga": "0x1.9102eceba2ad3p+1",
        "charm": "-0x1.4aae5736651b2p-3",
        "veta": "-0x1.86761af8e3b74p+8",
        "vera": "-0x1.0330930862045p+4",
        "speed": "-0x1.2cc4d1deb013cp-38",
        "zomma": "-0x1.172e00885bab3p-22",
        "color": "0x1.5510b7125fbc8p-21",
        "ultima": "-0x1.bb4626f6bd97bp+5",
        "elasticity": "0x1.69af4f069efd1p+4",
    },
    {
        "price": "0x1.64b94597498c6p+3",
        "delta": "-0x1.be3c9bf5b66c2p-2",
        "dual_delta": "0x1.d354e438149c9p-2",
        "vega": "0x1.140d33bf25eb5p+6",
        "theta": "-0x1.074e212a68118p+6",
        "rho": "-0x1.41c6a71cc1788p+4",
        "rho_dividend": "0x1.3624b6c7edc68p+4",
        "gamma": "0x1.4b4a087eab640p-9",
        "dual_gamma": "0x1.4de60ddfcfebfp-9",
        "vanna": "-0x1.2748ab7de16afp-5",
        "volga": "0x1.5d62da48e8a91p+1",
        "charm": "-0x1.ba2e28b5d64d0p-4",
        "veta": "-0x1.876397af335a4p+8",
        "vera": "-0x1.9d237a29a21bbp+3",
        "speed": "0x1.3a54fd771b83ep-7",
        "zomma": "0x1.0893c1452c2fdp-4",
        "color": "0x1.4515530efccf6p-1",
        "ultima": "-0x1.9f37f296d604ep+5",
        "elasticity": "-0x1.72782f0f67a87p+4",
    },
    {
        "price": "0x1.d060c612fba7ap-26",
        "delta": "-0x1.feadb4c570c9ap-28",
        "dual_delta": "0x1.30566790dc9b8p-27",
        "vega": "0x1.daac017cf6d3dp-19",
        "theta": "-0x1.51b3477f491e3p-16",
        "rho": "-0x1.ade9566138247p-24",
        "rho_dividend": "0x1.abb749500d61bp-24",
        "gamma": "0x1.4e58315ad3567p-41",
        "dual_gamma": "0x1.d524238bffffep-41",
        "vanna": "-0x1.1807c1a917617p-30",
        "volga": "0x1.cc2c4b84c7d6fp-19",
        "charm": "-0x1.3a995266a9be3p-25",
        "veta": "-0x1.8697bca8ad0a3p-14",
        "vera": "-0x1.a186adcc81107p-17",
        "speed": "-0x1.1dba419abb824p-33",
        "zomma": "0x1.9d2cf0d349672p-28",
        "color": "-0x1.312f18aa60e73p-25",
        "ultima": "-0x1.d5d4c3a0683eap-8",
        "elasticity": "-0x1.45aed59f0b718p+7",
    },
    {
        "price": "0x1.c2f399ec5b38dp+3",
        "delta": "0x1.324f6a9a54ae6p-2",
        "dual_delta": "-0x1.00ec5e8bbd227p-2",
        "vega": "0x1.1fdd79979e4d5p+7",
        "theta": "-0x1.039ba38e9c2ddp+5",
        "rho": "0x1.429f80e21098cp+6",
        "rho_dividend": "-0x1.5e6be3cbcba9fp+6",
        "gamma": "-0x1.f2d0c301da62ap-39",
        "dual_gamma": "-0x1.d95f6e94731fcp-39",
        "vanna": "-0x1.49a2f3b945cbfp-4",
        "volga": "-0x1.bc7d7265a52b3p+6",
        "charm": "-0x1.decc3f212ec76p-5",
        "veta": "-0x1.16ed3d70ca74bp+7",
        "vera": "0x1.143f20dcc257ep+8",
        "speed": "-0x1.9d8ea092321aep-35",
        "zomma": "0x1.1e23cd7cdd997p-23",
        "color": "0x1.60c75568569cap-27",
        "ultima": "0x1.a3f8b58152a35p+10",
        "elasticity": "0x1.92540fb39e6dcp+3",
    },
    {
        "price": "0x1.3c14a87fbc47fp+6",
        "delta": "0x1.7701d4b3ca647p-1",
        "dual_delta": "-0x1.4a4b7a09dc974p-1",
        "vega": "0x1.7cc32871d5e27p+7",
        "theta": "-0x1.c7d300d645600p+4",
        "rho": "0x1.5d4078d700673p+8",
        "rho_dividend": "-0x1.ac45a2ef0a29fp+8",
        "gamma": "0x1.1d0994010f13dp-38",
        "dual_gamma": "-0x1.4a9419637021ep-35",
        "vanna": "-0x1.46c7dcaa66dc1p-4",
        "volga": "0x1.d63113c3047ffp+6",
        "charm": "-0x1.f18435e0e9600p-6",
        "veta": "-0x1.5b0810873cb80p+6",
        "vera": "-0x1.2741057f89740p+9",
        "speed": "-0x1.77f606565c187p-32",
        "zomma": "0x1.46330e1896392p-22",
        "color": "0x0.0p+0",
        "ultima": "-0x1.d52e24e0a667fp+10",
        "elasticity": "0x1.5f5de28666dcap+2",
    },
    {
        "price": "0x1.2a2a06d76f120p+2",
        "delta": "-0x1.ba05bbaefa7a0p-2",
        "dual_delta": "0x1.e9ba8948b2800p-2",
        "vega": "0x1.b443226a35b61p+4",
        "theta": "-0x1.e3a473fc62940p+1",
        "rho": "-0x1.0bfe5ed453fc8p+4",
        "rho_dividend": "0x1.eaebc40d3c627p+3",
        "gamma": "0x1.069bb8b9ea000p-7",
        "dual_gamma": "0x1.0699274840000p-7",
        "vanna": "-0x1.b10243aa5cfffp-6",
        "volga": "0x1.841d4698127ffp-1",
        "charm": "-0x1.c8e4864cf6000p-5",
        "veta": "-0x1.a7691dd52be80p+4",
        "vera": "-0x1.31d362fce6f40p+3",
        "speed": "0x1.fe1092878f000p-5",
        "zomma": "-0x1.0256b16b41700p+1",
        "color": "-0x1.5fb2d17a96000p-2",
        "ultima": "-0x1.32dd9ad8796ffp+5",
        "elasticity": "-0x1.287ef9049a790p+3",
    },
    {
        "price": "0x0.0p+0",
        "delta": "0x0.0p+0",
        "dual_delta": "0x0.0p+0",
        "vega": "0x0.0p+0",
        "theta": "0x0.0p+0",
        "rho": "0x0.0p+0",
        "rho_dividend": "0x0.0p+0",
        "gamma": "0x0.0p+0",
        "dual_gamma": "0x0.0p+0",
        "vanna": "0x0.0p+0",
        "volga": "0x0.0p+0",
        "charm": "0x0.0p+0",
        "veta": "0x0.0p+0",
        "vera": "0x0.0p+0",
        "speed": "0x0.0p+0",
        "zomma": "0x0.0p+0",
        "color": "0x0.0p+0",
        "ultima": "0x0.0p+0",
        "elasticity": "-inf",
    },
]


@pytest.mark.parametrize("case,golden", zip(CASES, GOLDEN, strict=True))
def test_greeks_bit_exact(case: tuple, golden: dict[str, str]) -> None:
    S, K, T, r, sig, cp, q = case
    cat = AMER.greeks(S, K, T, r, sig, cp, q=q, conventions=CONV)
    for name in GREEK_NAMES:
        got = float.hex(float(getattr(cat, name)))
        assert got == golden[name], f"{name}: got {got}, golden {golden[name]}"


def test_bumped_trees_are_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shared-bump cache caps tree evaluations at the 28 unique bumps.

    Pre-refactor each higher-order greek re-bumped from scratch: 43
    evaluations per greeks() call. If this count climbs again, the drift
    canary's dominant cost climbs with it.
    """
    import pricing.engine as engine

    calls = 0
    real_crr_price = engine.crr_price

    def counting(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_crr_price(*args, **kwargs)

    monkeypatch.setattr(engine, "crr_price", counting)
    AMER.greeks(592.31, 590.0, 32 / 365, 0.043, 0.185, "call", q=0.013, conventions=CONV)
    assert calls == 28
