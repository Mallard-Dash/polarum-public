from __future__ import annotations

import copy
import unittest

from polarum.config import ConfigError, parse_config

VALID = {
    "settings": {"relay": "relay-host", "relay_broadcast": "192.168.1.255"},
    "nodes": [
        {"name": "node-a", "ip": "192.168.1.11", "mac": "aa:bb:cc:dd:ee:01",
         "update_order": 1, "canary": True, "health_ports": [22, 8006]},
        {"name": "node-b", "ip": "192.168.1.12", "mac": "AA:BB:CC:DD:EE:02",
         "update_order": 2},
    ],
}


def errors_for(mutate) -> list[str]:
    data = copy.deepcopy(VALID)
    mutate(data)
    with_err = None
    try:
        parse_config(data)
    except ConfigError as exc:
        with_err = exc.errors
    assert with_err is not None, "expected ConfigError"
    return with_err


class ConfigTests(unittest.TestCase):
    def test_valid_config_and_defaults(self) -> None:
        settings, nodes = parse_config(copy.deepcopy(VALID))
        self.assertEqual(settings.wake_timeout, 180.0)
        self.assertEqual(settings.update.reboot_policy, "manual")
        self.assertEqual(nodes[0].mac, "AA:BB:CC:DD:EE:01")  # normalized
        self.assertEqual(nodes[1].health_ports, (22,))
        self.assertEqual(nodes[1].ssh_host, "192.168.1.12")
        self.assertEqual(nodes[1].ssh_target, "polarum@192.168.1.12")
        self.assertTrue(nodes[0].canary)

    def test_bad_mac(self) -> None:
        errs = errors_for(lambda d: d["nodes"][0].update(mac="AA:BB:CC:DD:EE"))
        self.assertTrue(any("'mac'" in e for e in errs))

    def test_bad_ip(self) -> None:
        errs = errors_for(lambda d: d["nodes"][0].update(ip="192.168.1.300"))
        self.assertTrue(any("'ip'" in e for e in errs))

    def test_duplicates(self) -> None:
        def mutate(d):
            d["nodes"][1].update(name="NODE-A", ip="192.168.1.11",
                                 mac="AA:BB:CC:DD:EE:01", update_order=1)
        errs = " ".join(errors_for(mutate))
        for label in ("name", "ip", "mac", "update_order"):
            self.assertIn(f"duplicate {label}", errs)

    def test_unknown_keys_everywhere(self) -> None:
        def mutate(d):
            d["bogus"] = 1
            d["settings"]["typo"] = 1
            d["settings"]["update"] = {"nope": True}
            d["nodes"][0]["colour"] = "red"
        errs = " ".join(errors_for(mutate))
        for key in ("bogus", "typo", "nope", "colour"):
            self.assertIn(f"unknown key '{key}'", errs)

    def test_port_range(self) -> None:
        for ports in ([0], [65536], [], ["22"], [True], [22, 22]):
            errs = errors_for(lambda d, p=ports: d["nodes"][0].update(health_ports=p))
            self.assertTrue(any("health_ports" in e for e in errs), ports)

    def test_update_order_rules(self) -> None:
        errs = errors_for(lambda d: d["nodes"][0].update(update_order=0))
        self.assertTrue(any("update_order" in e for e in errs))
        errs = errors_for(lambda d: d["nodes"][1].update(canary=True, update_order=None)
                          or d["nodes"][1].pop("update_order"))
        self.assertTrue(any("requires an 'update_order'" in e for e in errs))
        errs = errors_for(lambda d: d["nodes"][0].update(update_order=True))
        self.assertTrue(any("update_order" in e for e in errs))

    def test_update_settings_validation(self) -> None:
        errs = " ".join(errors_for(lambda d: d["settings"].__setitem__(
            "update", {"reboot_policy": "sometimes", "require_snapshot": True,
                       "snapshot_before": False, "health_timeout": -1})))
        self.assertIn("reboot_policy", errs)
        self.assertIn("require_snapshot", errs)
        self.assertIn("health_timeout", errs)

    def test_relay_and_option_injection(self) -> None:
        errs = errors_for(lambda d: d["settings"].update(relay="-oProxyCommand=x"))
        self.assertTrue(any("relay" in e for e in errs))
        errs = errors_for(lambda d: d["nodes"][0].update(ssh_host="-oProxyCommand=x"))
        self.assertTrue(any("ssh_host" in e for e in errs))
        errs = errors_for(lambda d: d["settings"].update(relay_wol_command="wol; reboot"))
        self.assertTrue(any("relay_wol_command" in e for e in errs))

    def test_missing_pieces_and_all_errors_reported_together(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            parse_config({})
        self.assertTrue(any("[[nodes]]" in e for e in ctx.exception.errors))

        def mutate(d):
            d["nodes"][0].update(mac="bad", ip="bad")
        self.assertGreaterEqual(len(errors_for(mutate)), 2)

    def test_relay_is_optional(self) -> None:
        for relay in (None, ""):
            def mutate(d, r=relay):
                d["settings"].pop("relay")
                if r is not None:
                    d["settings"]["relay"] = r
            data = copy.deepcopy(VALID)
            mutate(data)
            settings, _ = parse_config(data)
            self.assertEqual(settings.relay, "")
        data = copy.deepcopy(VALID)
        del data["settings"]
        self.assertEqual(parse_config(data)[0].relay, "")
        errs = errors_for(lambda d: d["settings"].update(relay=5))
        self.assertTrue(any("'relay'" in e for e in errs))

    def test_wake_timeout(self) -> None:
        errs = errors_for(lambda d: d["settings"].update(wake_timeout="soon"))
        self.assertTrue(any("wake_timeout" in e for e in errs))


if __name__ == "__main__":
    unittest.main()
