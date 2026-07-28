"""
Unit tests for probe bug fixes:
  1. HeadroomPoolProbe: updateTestPortIdIp positional-arg fix (qosParams keyword)
  2. EgressDropProbe: lossy_queue fallback to dutQosConfig["param"] level
  3. UpperBound runaway guard: every probe must pass the pool_size= safety cap

These tests validate the fix logic in isolation without importing the full
TestQosProbe class (which requires heavy fixtures from QosSaiBase).
"""
import ast
import glob
import os

import pytest


# ---------------------------------------------------------------------------
# Fix 1: updateTestPortIdIp positional-arg
# ---------------------------------------------------------------------------
# The function signature is:
#   updateTestPortIdIp(self, dutConfig, get_src_dst_asic_and_duts,
#                      portSpeedCableLength=None, qosParams=None)
#
# Bug: the call site passed qosConfig["hdrm_pool_size"] as the 3rd positional
#      arg, binding it to portSpeedCableLength instead of qosParams.
# Fix: use keyword arg qosParams=qosConfig["hdrm_pool_size"]

def _simulate_updateTestPortIdIp(portSpeedCableLength=None, qosParams=None):
    """Simulate the function to verify which parameter receives the value."""
    return {"portSpeedCableLength": portSpeedCableLength, "qosParams": qosParams}


class TestPositionalArgFix:
    """Verify that hdrm_pool_size dict reaches qosParams, not portSpeedCableLength."""

    def test_buggy_positional_call(self):
        """OLD code: 3rd positional arg goes to portSpeedCableLength (wrong)."""
        hdrm_data = {"src_port_ids": [0, 1], "dst_port_id": 2}
        result = _simulate_updateTestPortIdIp(hdrm_data)  # positional
        assert result["portSpeedCableLength"] == hdrm_data, \
            "Positional call should bind to portSpeedCableLength (the bug)"
        assert result["qosParams"] is None

    def test_fixed_keyword_call(self):
        """NEW code: keyword arg goes to qosParams (correct)."""
        hdrm_data = {"src_port_ids": [0, 1], "dst_port_id": 2}
        result = _simulate_updateTestPortIdIp(qosParams=hdrm_data)  # keyword
        assert result["qosParams"] == hdrm_data, \
            "Keyword call should bind to qosParams (the fix)"
        assert result["portSpeedCableLength"] is None

    def test_both_params_independent(self):
        """Both parameters can be set independently."""
        result = _simulate_updateTestPortIdIp(
            portSpeedCableLength="100000_3m", qosParams={"key": "val"})
        assert result["portSpeedCableLength"] == "100000_3m"
        assert result["qosParams"] == {"key": "val"}

    def test_no_args_defaults(self):
        """Both default to None."""
        result = _simulate_updateTestPortIdIp()
        assert result["portSpeedCableLength"] is None
        assert result["qosParams"] is None


# ---------------------------------------------------------------------------
# Fix 2: EgressDropProbe lossy_queue fallback
# ---------------------------------------------------------------------------
# Replicate the fixed lookup logic from testQosEgressDropProbe:
#   1) Look in speed-specific qosConfig (dutQosConfig["param"][speedCableLen])
#   2) If not found, fall back to dutQosConfig["param"] (top-level)
#   3) If still not found, pytest.skip
#
# Keep in sync with: tests/qos/test_qos_probe.py :: testQosEgressDropProbe

def _resolve_lossy_profile(dutQosConfig, portSpeedCableLength, lossyProfile):
    """Replicate the fixed lossy profile lookup logic.

    Returns (qosConfig_dict, skipped_reason_or_None).
    """
    qosConfig = dutQosConfig["param"][portSpeedCableLength]
    if lossyProfile not in qosConfig:
        qosConfig = dutQosConfig["param"]
        if lossyProfile not in qosConfig:
            return None, f"{lossyProfile} is not defined in QoS config"
    return qosConfig, None


class TestLossyQueueFallback:
    """Verify lossy_queue_1 lookup with 2-level fallback."""

    @pytest.fixture
    def broadcom_config(self):
        """Broadcom: lossy_queue_1 is inside the speed-specific sub-dict."""
        return {
            "param": {
                "100000_3m": {
                    "lossy_queue_1": {"dscp": 8, "ecn": 1, "pg": 0},
                    "other_key": "value",
                },
            }
        }

    @pytest.fixture
    def mellanox_config(self):
        """Mellanox: lossy_queue_1 is at the top-level param dict,
        not inside the per-speed sub-dict."""
        return {
            "param": {
                "100000_3m": {
                    "headroom_pool_size": "1024",
                },
                "lossy_queue_1": {"dscp": 8, "ecn": 1, "pg": 0},
            }
        }

    @pytest.fixture
    def missing_config(self):
        """Neither level has lossy_queue_1."""
        return {
            "param": {
                "100000_3m": {
                    "headroom_pool_size": "1024",
                },
            }
        }

    def test_broadcom_direct_lookup(self, broadcom_config):
        """Broadcom: found in speed-specific sub-dict (no fallback needed)."""
        cfg, skip = _resolve_lossy_profile(broadcom_config, "100000_3m", "lossy_queue_1")
        assert skip is None
        assert "lossy_queue_1" in cfg
        assert cfg is broadcom_config["param"]["100000_3m"]

    def test_mellanox_fallback_to_param(self, mellanox_config):
        """Mellanox: NOT in speed sub-dict, found at param level (fallback)."""
        cfg, skip = _resolve_lossy_profile(mellanox_config, "100000_3m", "lossy_queue_1")
        assert skip is None
        assert "lossy_queue_1" in cfg
        assert cfg is mellanox_config["param"]

    def test_missing_skip(self, missing_config):
        """Neither level has lossy_queue_1 → skip."""
        cfg, skip = _resolve_lossy_profile(missing_config, "100000_3m", "lossy_queue_1")
        assert cfg is None
        assert "not defined" in skip

    def test_mellanox_speed_dict_untouched(self, mellanox_config):
        """Verify fallback doesn't modify the speed-specific dict."""
        speed_dict_before = dict(mellanox_config["param"]["100000_3m"])
        _resolve_lossy_profile(mellanox_config, "100000_3m", "lossy_queue_1")
        assert mellanox_config["param"]["100000_3m"] == speed_dict_before

    def test_custom_profile_name(self):
        """Fallback works for any profile name, not just lossy_queue_1."""
        config = {
            "param": {
                "50000_1m": {},
                "custom_lossy": {"dscp": 10},
            }
        }
        cfg, skip = _resolve_lossy_profile(config, "50000_1m", "custom_lossy")
        assert skip is None
        assert cfg["custom_lossy"]["dscp"] == 10

    def test_profile_in_both_levels_prefers_speed(self):
        """If profile exists in BOTH levels, speed-specific takes precedence."""
        config = {
            "param": {
                "100000_3m": {
                    "lossy_queue_1": {"dscp": 8, "source": "speed"},
                },
                "lossy_queue_1": {"dscp": 8, "source": "param"},
            }
        }
        cfg, skip = _resolve_lossy_profile(config, "100000_3m", "lossy_queue_1")
        assert skip is None
        assert cfg["lossy_queue_1"]["source"] == "speed", \
            "Speed-specific config should take precedence over param-level"


class TestLossyQueueEdgeCases:
    """Edge cases for the fallback logic."""

    def test_empty_speed_dict(self):
        """Speed dict is empty → falls back to param level."""
        config = {
            "param": {
                "100000_3m": {},
                "lossy_queue_1": {"dscp": 8},
            }
        }
        cfg, skip = _resolve_lossy_profile(config, "100000_3m", "lossy_queue_1")
        assert skip is None
        assert cfg is config["param"]

    def test_empty_param_dict_skips(self):
        """Param-level also has nothing → skip."""
        config = {
            "param": {
                "100000_3m": {},
            }
        }
        _, skip = _resolve_lossy_profile(config, "100000_3m", "lossy_queue_1")
        assert skip is not None

    def test_breakout_sku_scenario(self):
        """Simulates breakout SKU where config comes from ["breakout"] sub-key.
        The fallback still applies the same way."""
        config = {
            "param": {
                "100000_3m": {
                    "breakout": {
                        # In real code, qosConfig = this breakout dict
                    }
                },
                "lossy_queue_1": {"dscp": 8},
            }
        }
        # Simulating: qosConfig = config["param"]["100000_3m"]["breakout"]
        breakout_config = config["param"]["100000_3m"]["breakout"]
        if "lossy_queue_1" not in breakout_config:
            fallback = config["param"]
            assert "lossy_queue_1" in fallback


# ---------------------------------------------------------------------------
# Fix 3: UpperBound runaway guard - every probe must pass the pool_size= cap
# ---------------------------------------------------------------------------
# Bug (2026-07): pfc_xon_probing.py and egress_drop_probing.py invoked the
# exponential UpperBoundProbingAlgorithm.run() WITHOUT the pool_size= safety
# cap. Without that kwarg the cap ("abort if current > 3x pool_size") is
# disabled, so if the threshold is never detected the fill count doubles up to
# max_iterations=20 (pool_size * 2^k), sending an astronomically large number
# of single packets that never completes on large-buffer platforms (Cisco 8000
# pfcxoff_point is up to ~762k packets).
#
# The algorithm INTENTIONALLY keeps pool_size optional -- the mock unit tests
# (test_upper_bound_probing_algorithm.py) exercise the uncapped exponential /
# max-iteration paths on purpose. Enforcement therefore lives at the CALLER
# layer: every production probe that drives UpperBoundProbingAlgorithm.run()
# MUST pass pool_size=. This static AST guard fails if any probe drops the
# kwarg again (or a newly-added probe forgets it).
#
# Keep in sync with: tests/saitests/probe/*_probing.py upper-bound call sites.

def _probe_dir():
    """Absolute path to tests/saitests/probe (this file lives in .../mock/ut)."""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, "..", "..", "probe"))


def _slice_const_str(slice_node):
    """Return the string key of a subscript slice across Python versions.

    py3.9+ stores the expression directly in Subscript.slice; older versions
    wrap it in ast.Index. Returns None if the slice is not a string constant.
    """
    index_cls = getattr(ast, "Index", None)
    if index_cls is not None and isinstance(slice_node, index_cls):
        slice_node = slice_node.value
    if isinstance(slice_node, ast.Constant) and isinstance(slice_node.value, str):
        return slice_node.value
    return None


def _iter_upper_bound_run_calls(tree):
    """Yield ast.Call nodes that invoke `.run(...)` on an UpperBound algorithm.

    Recognizes the two call shapes used across the probes:
      A) UpperBoundProbingAlgorithm(...).run(...)     # inline chain (pfc_xon)
      B) <collection>["upper..."].run(...)            # dict-held instance
         e.g. algorithms["upper_bound"], pfc_algos['upper'], drop_algos['upper']
    """
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "run"):
            continue
        recv = node.func.value

        # Case A: chained directly on a fresh UpperBoundProbingAlgorithm(...)
        if isinstance(recv, ast.Call):
            f = recv.func
            name = getattr(f, "id", None) or getattr(f, "attr", None)
            if name == "UpperBoundProbingAlgorithm":
                yield node
            continue

        # Case B: subscript into a collection using an 'upper' key
        if isinstance(recv, ast.Subscript):
            key = _slice_const_str(recv.slice)
            if key is not None and "upper" in key.lower():
                yield node


class TestUpperBoundPoolSizeCapGuard:
    """Every production probe must pass pool_size= to UpperBound.run() so the
    exponential search can never grow unbounded (2026-07 runaway regression)."""

    def _upper_bound_calls(self):
        """Collect (filename, ast.Call) for every UpperBound.run() call site."""
        calls = []
        for path in sorted(glob.glob(os.path.join(_probe_dir(), "*_probing.py"))):
            with open(path) as fh:
                tree = ast.parse(fh.read(), filename=path)
            for node in _iter_upper_bound_run_calls(tree):
                calls.append((os.path.basename(path), node))
        return calls

    def test_scanner_finds_upper_bound_calls(self):
        """Sanity: the AST scanner actually locates the known call sites.

        There are currently 6 upper-bound run sites (pfc_xoff, pfc_xon,
        ingress_drop, egress_drop, and headroom_pool x2). Guard against the
        scanner silently matching nothing after a call-shape refactor.
        """
        calls = self._upper_bound_calls()
        assert len(calls) >= 5, (
            f"Expected to locate the UpperBound.run() call sites across the "
            f"probes but found {len(calls)}. Did the call shape change? "
            f"Update _iter_upper_bound_run_calls()."
        )

    def test_every_upper_bound_run_passes_pool_size(self):
        """Regression guard: no probe may invoke UpperBound.run() without the
        pool_size= safety cap (the 2026-07 unbounded-fill runaway)."""
        offenders = []
        for fname, node in self._upper_bound_calls():
            has_cap = any(kw.arg == "pool_size" for kw in node.keywords)
            if not has_cap:
                offenders.append(f"{fname}:{node.lineno}")
        assert not offenders, (
            "UpperBoundProbingAlgorithm.run() called WITHOUT the pool_size= "
            "safety cap (unbounded exponential fill - see 2026-07 runaway "
            "bug). Add pool_size=pool_size to:\n  " + "\n  ".join(offenders)
        )
