"""Unit tests for the TX level model and the safe command ordering (no Qt needed)."""
import random
import unittest

import _paths  # noqa: F401
from bench.sim_tx import SimTxTeensy
from bench.tx_model import Level, est_vpp, parse_status, plan_level_commands, pot_gain


class TestLevelModel(unittest.TestCase):
    def test_spec_table(self):
        # spec §3.6: Vpp_diff per 100 mV pk of DAC amplitude
        table = {0: (1.010, 5.52), 64: (0.760, 4.15), 128: (0.510, 2.79), 192: (0.260, 1.42),
                 224: (0.135, 0.74), 240: (0.0725, 0.40), 255: (0.0139, 0.08)}
        for d, (g, v) in table.items():
            self.assertAlmostEqual(pot_gain(d), g, delta=0.0006, msg=f"D={d}")
            self.assertAlmostEqual(est_vpp(100, d), v, delta=0.006, msg=f"D={d}")

    def test_first_test_example(self):
        # A_dac = 200 mV pk, D = 200 -> G = 0.229 -> ≈ 2.5 Vpp
        self.assertAlmostEqual(pot_gain(200), 0.229, delta=0.001)
        self.assertAlmostEqual(est_vpp(200, 200), 2.50, delta=0.01)

    def test_parse_status(self):
        st = parse_status("STATUS armed=1 run=0 freq=25000 amp=200 pot=200 mode=CONT on_us=1000 "
                          "per_us=100000 est=2.50 limit=20.0 power=1")
        self.assertEqual(st["armed"], "1")
        self.assertEqual(st["mode"], "CONT")
        self.assertEqual(Level.from_status(st), Level(200, 200, 20.0, 1.0))


def random_level(rng, within=True):
    while True:
        lv = Level(rng.choice([0, 10, 100, 200, 500, 900, 1400, rng.randint(0, 1400)]),
                   rng.choice([0, 64, 128, 200, 240, 255, rng.randint(0, 255)]),
                   round(rng.choice([1.0, 5.0, 10.0, 20.0, 50.0, rng.uniform(1, 50)]), 1),
                   round(rng.choice([1.0, 0.5, 2.0, rng.uniform(0.5, 2.0)]), 3))
        if not within or lv.within_limit():
            return lv


class TestPlan(unittest.TestCase):
    def run_plan_on_sim(self, current, target, known=True):
        sim = SimTxTeensy()
        # put the simulated firmware into `current`
        sim.amp, sim.pot, sim.limit, sim.cal = current.amp, current.pot, current.limit, current.cal
        cmds = plan_level_commands(current if known else None, target)
        for c in cmds:
            r = sim.handle(c)
            self.assertTrue(r and r[0].startswith("OK"), f"{current} -> {target}: {c} -> {r}  plan={cmds}")
        self.assertEqual((sim.amp, sim.pot, round(sim.limit, 1), round(sim.cal, 3)),
                         (target.amp, target.pot, target.limit, target.cal))
        return cmds

    def test_random_transitions_never_rejected(self):
        rng = random.Random(1234)
        for _ in range(4000):
            cur, tgt = random_level(rng), random_level(rng)
            self.run_plan_on_sim(cur, tgt, known=True)
            self.run_plan_on_sim(cur, tgt, known=False)

    def test_only_changed_sent(self):
        a = Level(200, 200, 20.0, 1.0)
        self.assertEqual(plan_level_commands(a, a), [])
        self.assertEqual(plan_level_commands(a, Level(300, 200, 20.0, 1.0)), ["AMP 300"])

    def test_raise_limit_first_lower_last(self):
        cur = Level(900, 0, 50.0, 1.0)             # 49.8 Vpp
        tgt = Level(100, 0, 10.0, 1.0)             # 5.5 Vpp, limit lowered
        self.assertEqual(plan_level_commands(cur, tgt), ["AMP 100", "LIMIT 10.0"])
        cur, tgt = tgt, Level(900, 0, 50.0, 1.0)
        self.assertEqual(plan_level_commands(cur, tgt), ["LIMIT 50.0", "AMP 900"])

    def test_amp_pot_order(self):
        # more amplitude, less gain -> POT first
        cur, tgt = Level(200, 0, 20.0), Level(1400, 240, 20.0)
        self.assertEqual(plan_level_commands(cur, tgt), ["POT 240", "AMP 1400"])
        # less amplitude, more gain -> AMP first
        self.assertEqual(plan_level_commands(tgt, cur), ["AMP 200", "POT 0"])

    def test_unknown_state_silences_first(self):
        cmds = plan_level_commands(None, Level(200, 200, 20.0, 1.0))
        self.assertEqual(cmds[0], "AMP 0")
        self.assertEqual(cmds[-1], "AMP 200")


if __name__ == "__main__":
    unittest.main()
